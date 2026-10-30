"""Shell health and cleanup must agree with the selected SDK launcher."""

from __future__ import annotations

from importlib.metadata import distribution
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugin" / "scripts"


def _locked_reflexio_version() -> str:
    lock = tomllib.loads((ROOT / "plugin" / "uv.lock").read_text())
    (version,) = {p["version"] for p in lock["package"] if p["name"] == "reflexio-ai"}
    return version


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


@pytest.mark.parametrize("crlf", [False, True])
@pytest.mark.parametrize(
    "endpoint", [None, "http://127.0.0.1:9999", "https://owned.invalid"]
)
def test_health_and_cleanup_follow_real_sdk_ownership(tmp_path, endpoint, crlf):
    _run_checks(tmp_path, legacy=None, endpoint=endpoint, failure=None, crlf=crlf)


@pytest.mark.parametrize("failure", ["exit", "invalid"])
def test_unknown_ownership_is_not_healthy_and_never_kills(tmp_path, failure):
    _run_checks(tmp_path, legacy=None, endpoint=None, failure=failure)


@pytest.mark.parametrize("legacy", ["", "0", "1", "private-not-to-log"])
@pytest.mark.parametrize(
    "endpoint", [None, "http://127.0.0.1:9999", "https://owned.invalid"]
)
def test_retired_setting_fails_ownership_before_process_actions(
    tmp_path, legacy, endpoint
):
    _run_checks(tmp_path, legacy=legacy, endpoint=endpoint, failure=None)


@pytest.mark.parametrize("legacy", [None, "1"])
def test_ownership_probe_also_supports_installed_sdk_baseline(tmp_path, legacy):
    _run_checks(tmp_path, legacy=legacy, endpoint=None, failure=None, installed=True)


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize(
    "assignment",
    [
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING",
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING=",
        'CLAUDE_SMART_USE_LOCAL_EMBEDDING="  "',
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING=0",
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1",
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING=\nPYTHON_DOTENV_DISABLED=yes",
    ],
)
def test_selected_file_retired_keys_rejected_before_pruning(
    tmp_path, installed, assignment
):
    _run_checks(
        tmp_path,
        legacy=None,
        endpoint=None,
        failure=None,
        installed=installed,
        file_assignment=assignment,
    )


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize(
    "assignment",
    ["CLAUDE_SMART_USE_LOCAL_EMBEDDING=", "CLAUDE_SMART_USE_LOCAL_EMBEDDING"],
)
def test_disabled_dotenv_ignores_retired_file_keys(tmp_path, installed, assignment):
    _run_checks(
        tmp_path,
        legacy=None,
        endpoint="https://ignored.invalid",
        failure=None,
        installed=installed,
        file_assignment=assignment,
        disabled=True,
    )


def _run_checks(
    tmp_path,
    *,
    legacy,
    endpoint,
    failure,
    crlf=False,
    installed=False,
    file_assignment=None,
    disabled=False,
):
    vendor = (
        Path(str(distribution("reflexio-ai").locate_file("")))
        if installed
        else ROOT / "plugin" / "vendor" / "reflexio"
    )
    assert (vendor / "reflexio").is_dir(), (
        "Materialize the selected SDK before this gate"
    )
    if installed:
        # Identity, not a file probe: the PyPI baseline may ship any module the
        # vendor has, so check that the locked release came from the index.
        dist = distribution("reflexio-ai")
        assert (
            dist.read_text("direct_url.json") is None
            and dist.version == _locked_reflexio_version()
        ), (
            "Baseline compatibility requires the locked PyPI SDK, not an installed "
            "vendor; run uv sync --project plugin --locked before this gate"
        )
    else:
        assert (vendor / "reflexio" / "removed_env.py").is_file()

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
            else (
                "import io,runpy\n"
                + (
                    "sys.stdout=io.TextIOWrapper(sys.stdout.buffer, newline='\\r\\n')\n"
                    if crlf
                    else ""
                )
                + "runpy.run_path(sys.argv[1], run_name='__main__')\n"
                + f"assert Path(sys.modules['reflexio'].__file__).resolve().is_relative_to(Path({str(vendor)!r}).resolve())\n"
            )
        )
    )
    python.chmod(0o700)
    env = os.environ.copy()
    for key in (
        "REFLEXIO_EMBEDDING_SERVICE_URL",
        "REFLEXIO_EMBEDDING_DAEMON_HOST",
        "REFLEXIO_DISABLE_LOCAL_EMBEDDING_DAEMON",
        "PYTHON_DOTENV_DISABLED",
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING",
    ):
        env.pop(key, None)
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        REFLEXIO_ENV_FILE=str(tmp_path / "selected.env"),
        OWNED_ACTIONS=str(actions),
    )
    env["PYTHONPATH"] = os.pathsep.join([str(vendor), env.get("PYTHONPATH", "")])
    if legacy is not None:
        env["CLAUDE_SMART_USE_LOCAL_EMBEDDING"] = legacy
    if disabled:
        env["PYTHON_DOTENV_DISABLED"] = "yes"
    # Endpoint and retired keys come from the actual SDK-selected file.
    selected = tmp_path / "selected.env"
    if endpoint or file_assignment is not None:
        selected.write_text(
            (f"REFLEXIO_EMBEDDING_SERVICE_URL={endpoint}\n" if endpoint else "")
            + (file_assignment + "\n" if file_assignment is not None else "")
        )
    before = selected.read_bytes() if selected.exists() else None
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
    assert (selected.read_bytes() if selected.exists() else None) == before
    if before is None:
        assert not (tmp_path / ".reflexio").exists()
    if failure or legacy is not None or (file_assignment is not None and not disabled):
        assert result.stdout.splitlines() == ["failed"] * 3
        if legacy is not None or file_assignment is not None:
            assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING" in result.stderr
            assert "private-not-to-log" not in result.stderr
        assert "skipping embedding cleanup" in result.stderr
        assert not actions.exists()
    elif endpoint and not disabled:
        assert result.stdout.splitlines() == ["ok"] * 3
        assert not actions.exists()
    else:
        assert result.stdout.splitlines() == ["ok"] * 3
        assert actions.read_text().splitlines() == ["inspect"] * 3 + ["stop"]
