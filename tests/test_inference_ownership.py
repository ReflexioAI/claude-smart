"""Shell health and cleanup must agree with the selected SDK launcher."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugin" / "scripts"


def _functions() -> str:
    source = (SCRIPTS / "backend-service.sh").read_text()
    functions = []
    for name in (
        "resolve_inference_ownership",
        "embedding_owned_by_current_vendor",
        "embedding_current_or_compatible",
        "stop_stale_embedding_listener_if_owned",
    ):
        match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}", source)
        assert match, name
        functions.append(match.group())
    return "\n".join(functions)


@pytest.mark.parametrize("legacy", ["0", "1"])
@pytest.mark.parametrize(
    "endpoint", [None, "http://127.0.0.1:9999", "https://owned.invalid"]
)
def test_health_and_cleanup_follow_real_sdk_ownership(tmp_path, legacy, endpoint):
    _run_checks(tmp_path, legacy=legacy, endpoint=endpoint, failure=None)


@pytest.mark.parametrize("failure", ["exit", "invalid"])
def test_unknown_ownership_is_not_healthy_and_never_kills(tmp_path, failure):
    _run_checks(tmp_path, legacy="1", endpoint=None, failure=failure)


def _run_checks(tmp_path, *, legacy, endpoint, failure):
    calls = tmp_path / "queries"
    actions = tmp_path / "actions"
    python = tmp_path / "selected-python"
    # Only the wrapper is fake: successful queries execute the actual installed
    # SDK and real adapter. Never launch a daemon, inspect real PIDs or send signals.
    python.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write('query\\n')\n"
        + (
            "raise SystemExit(1)\n"
            if failure == "exit"
            else "print('invalid ownership')\n"
            if failure == "invalid"
            else f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        )
    )
    python.chmod(0o700)
    env = os.environ.copy()
    for key in (
        "REFLEXIO_EMBEDDING_SERVICE_URL",
        "REFLEXIO_EMBEDDING_DAEMON_HOST",
        "REFLEXIO_DISABLE_LOCAL_EMBEDDING_DAEMON",
        "PYTHON_DOTENV_DISABLED",
    ):
        env.pop(key, None)
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        REFLEXIO_ENV_FILE=str(tmp_path / "selected.env"),
        CLAUDE_SMART_USE_LOCAL_EMBEDDING=legacy,
        OWNED_ACTIONS=str(actions),
    )
    # Endpoint comes from the same explicitly selected file the launcher reads.
    if endpoint:
        (tmp_path / "selected.env").write_text(
            f"REFLEXIO_EMBEDDING_SERVICE_URL={endpoint}\n"
        )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / ".env").write_text(
        "REFLEXIO_EMBEDDING_SERVICE_URL=https://must-not-use.invalid\n"
    )
    script = (
        "set -eu\n"
        f"HERE={shlex.quote(str(SCRIPTS))}\n"
        f"PLUGIN_ROOT={shlex.quote(str(runtime_root))}\n"
        'EMBEDDING_PORT=8072\ninference_ownership=""\n'
        "prepare_backend_import_context() {\n"
        f"  backend_python={shlex.quote(str(python))}\n"
        '  backend_pythonpath="${PYTHONPATH:-}"\n}\n'
        'claude_smart_port_listener_pids() { echo inspect >> "$OWNED_ACTIONS"; echo 12345; }\n'
        "is_claude_smart_backend_pid() { return 0; }\n"
        "pid_uses_current_vendor_root() { return 0; }\n"
        "backend_pid_current_or_compatible() { return 0; }\n"
        "backend_pid_strictly_older_than_plugin() { return 0; }\n"
        'stop_backend_pids() { echo stop >> "$OWNED_ACTIONS"; }\n'
        + _functions()
        + "\nfor check in embedding_owned_by_current_vendor embedding_current_or_compatible stop_stale_embedding_listener_if_owned; do\n"
        '  if "$check"; then echo ok; else echo failed; fi\ndone\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        cwd=foreign,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert calls.read_text().splitlines() == ["query"], result.stderr
    if not endpoint:
        assert not (tmp_path / "selected.env").exists()
        assert not (tmp_path / ".reflexio").exists()
    if failure:
        assert result.stdout.splitlines() == ["failed"] * 3
        assert "skipping embedding cleanup" in result.stderr
        assert not actions.exists()
    elif endpoint:
        assert result.stdout.splitlines() == ["ok"] * 3
        assert not actions.exists()
    else:
        assert result.stdout.splitlines() == ["ok"] * 3
        assert actions.read_text().splitlines() == ["inspect"] * 3 + ["stop"]
