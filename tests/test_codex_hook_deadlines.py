"""Real launcher dispatch with private state and no services or model calls."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "plugin/scripts/codex-hook.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    not NODE or os.name == "nt", reason="POSIX executable fixtures"
)


def project(tmp_path: Path, python_body: str | None):
    root = tmp_path / "plugin"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname="diagnostic"\nversion="0.0.0"\n'
    )
    launcher = scripts / "codex-hook.js"
    shutil.copyfile(SOURCE, launcher)
    if python_body is not None:
        python = root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f"#!{sys.executable}\n{python_body}\n")
        python.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    # A live hook must not invoke uv, even when an executable is available.
    fake_uv = home / ".local/bin/uv"
    fake_uv.parent.mkdir(parents=True)
    fake_uv.write_text(
        f"#!{sys.executable}\nraise RuntimeError('uv must not run in a hook')\n"
    )
    fake_uv.chmod(0o700)
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "PLUGIN_ROOT": str(root),
        "DIAGNOSTIC_ARGS": str(tmp_path / "args.json"),
    }
    return launcher, home, env


def run(launcher, env, event="post-tool"):
    return subprocess.run(
        [str(NODE), str(launcher), "hook", event],
        input=json.dumps(
            {"prompt": "private diagnostic input", "session_id": "diagnostic"}
        ),
        env=env,
        text=True,
        capture_output=True,
        timeout=17,
    )


def test_prepared_python_preserves_hook_context_without_uv(tmp_path):
    launcher, home, env = project(
        tmp_path,
        """import json, os, pathlib, sys
pathlib.Path(os.environ["DIAGNOSTIC_ARGS"]).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"continue": True, "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "synthetic learning"}}))""",
    )
    result = run(launcher, env, "user-prompt")
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        == "synthetic learning"
    )
    assert json.loads(Path(env["DIAGNOSTIC_ARGS"]).read_text()) == [
        "-m",
        "claude_smart.hook",
        "codex",
        "user-prompt",
    ]
    trace = json.loads((home / ".claude-smart/hook-launch.log").read_text())
    assert trace["outcome"] == "completed" and trace["budget_ms"] == 12000
    assert trace["duration_ms"] >= 0
    assert "private diagnostic input" not in json.dumps(trace)


def test_missing_environment_returns_visible_degraded_result(tmp_path):
    launcher, home, env = project(tmp_path, None)
    result = run(launcher, env)
    assert result.returncode == 0
    assert "Prepared Python is missing" in json.loads(result.stdout)["systemMessage"]
    assert (
        json.loads((home / ".claude-smart/hook-launch.log").read_text())["outcome"]
        == "not_ready"
    )
    assert not Path(env["DIAGNOSTIC_ARGS"]).exists()


def test_unstartable_environment_is_reported_without_payload(tmp_path):
    launcher, home, env = project(tmp_path, 'print("unused")')
    python = launcher.parents[1] / ".venv/bin/python"
    python.chmod(0o600)
    result = run(launcher, env)
    assert result.returncode == 0
    assert "could not start" in json.loads(result.stdout)["systemMessage"]
    assert (
        json.loads((home / ".claude-smart/hook-launch.log").read_text())["outcome"]
        == "spawn_failed"
    )


def test_missing_environment_requests_detached_installer_recovery(tmp_path):
    launcher, home, env = project(tmp_path, None)
    installer = launcher.parent / "smart-install.sh"
    installer.write_text(
        '#!/bin/sh\nprintf "%s" "$CLAUDE_SMART_BOOTSTRAPPING" > "$HOME/recovery-requested"\n'
    )
    result = run(launcher, env)
    assert result.returncode == 0
    marker = home / "recovery-requested"
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.read_text() == "1"
    assert json.loads(result.stdout)["continue"] is True


def test_hung_python_ignoring_sigterm_is_killed_before_host_limit(tmp_path):
    launcher, home, env = project(
        tmp_path,
        """import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)""",
    )
    result = run(launcher, env)
    assert result.returncode == 0
    assert "12s execution budget" in json.loads(result.stdout)["systemMessage"]
    trace = json.loads((home / ".claude-smart/hook-launch.log").read_text())
    assert trace["outcome"] == "timeout" and trace["budget_ms"] == 12000
    assert trace["duration_ms"] < 15000


def test_real_stop_returns_before_detached_publication_and_preserves_ack_boundary(
    tmp_path, session_dir
):
    from claude_smart import state

    # Only the SDK transport is fake: real Node, prepared Python, dispatcher,
    # Stop, outbox and detached worker all execute. No server or provider call.
    sdk = tmp_path / "fake-sdk/reflexio"
    sdk.mkdir(parents=True)
    sdk.joinpath("__init__.py").write_text("""import json, os, pathlib, time
from types import SimpleNamespace
class ReflexioClient:
    def __init__(self, **kwargs): pass
    def publish_interaction(self, **kwargs):
        root = pathlib.Path(os.environ["DIAGNOSTIC_GATE"])
        (root / "started").touch()
        for _ in range(100):
            if (root / "allow").exists(): break
            time.sleep(0.05)
        else: raise RuntimeError("Diagnostic acknowledgement was not released")
        (root / "saved.json").write_text(json.dumps(kwargs))
        return SimpleNamespace(success=True, request_id="diagnostic-confirmed")
""")
    home = tmp_path / "home"
    home.mkdir()
    gate = tmp_path / "gate"
    gate.mkdir()
    project_dir = tmp_path / "external-project"
    project_dir.mkdir()
    state.append("real-stop", {"role": "User", "content": "synthetic prompt"})
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CLAUDE_PLUGIN_ROOT": str(SOURCE.parents[1]),
        "PLUGIN_ROOT": str(SOURCE.parents[1]),
        "CODEX_HOME": str(home / ".codex"),
        "PYTHONPATH": str(sdk.parent),
        "CLAUDE_SMART_INTERNAL": "0",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "REFLEXIO_URL": "http://127.0.0.1:1",
        "REFLEXIO_API_KEY": "diagnostic-only",
        "DIAGNOSTIC_GATE": str(gate),
        "CLAUDE_SMART_HOOK_LOG": str(tmp_path / "hook.log"),
    }
    try:
        result = subprocess.run(
            [str(NODE), str(SOURCE), "hook", "stop"],
            env=env,
            input=json.dumps(
                {
                    "session_id": "real-stop",
                    "cwd": str(project_dir),
                    "last_assistant_message": "synthetic answer",
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["continue"] is True
        assert not (gate / "saved.json").exists()
        assert state.published_record_offset(state.read_all("real-stop")) == 0
        assert list((session_dir / "publish-outbox").glob("*.json"))
    finally:
        (gate / "allow").touch()
    deadline = time.monotonic() + 8
    jobs = list((session_dir / "publish-outbox").glob("*.json"))
    while time.monotonic() < deadline:
        jobs = list((session_dir / "publish-outbox").glob("*.json"))
        if (gate / "saved.json").exists() and not jobs:
            break
        time.sleep(0.02)
    assert (gate / "started").exists()
    saved = json.loads((gate / "saved.json").read_text())
    assert [item["content"] for item in saved["interactions"]] == [
        "synthetic prompt",
        "synthetic answer",
    ]
    assert state.published_record_offset(state.read_all("real-stop")) > 0
    assert not jobs
