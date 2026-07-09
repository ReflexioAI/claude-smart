"""Tests for the Reflexio LocalScriptAssistant bridge."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from claude_smart import optimizer_assistant, runtime


def _run_main(monkeypatch, payload):
    stdout = io.StringIO()
    stderr = io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    rc = optimizer_assistant.main()
    return rc, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("key", ["result", "response"])
def test_optimizer_assistant_invokes_claude_and_emits_content(monkeypatch, key) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({key: "assistant reply"}),
            stderr="",
        )

    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which", lambda _: "/bin/claude"
    )
    monkeypatch.setattr("claude_smart.optimizer_assistant.subprocess.run", fake_run)

    rc, stdout, stderr = _run_main(
        monkeypatch,
        {
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "seen"},
                {"role": "user", "content": "answer now"},
            ],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": "summaries"}],
        },
    )

    assert rc == 0
    assert stderr == ""
    assert json.loads(stdout) == {"content": "assistant reply"}
    cmd, kwargs = calls[0]
    assert cmd[:3] == ["/bin/claude", "-p", "--output-format"]
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,LS"
    assert (
        cmd[cmd.index("--disallowedTools") + 1]
        == "Bash,Edit,Write,MultiEdit,NotebookEdit"
    )
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers": {}}'
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--allow-dangerously-skip-permissions" not in cmd
    assert kwargs["input"] == "answer now"
    assert kwargs["env"]["CLAUDE_SMART_INTERNAL"] == "1"
    assert kwargs["env"]["CLAUDE_CODE_ENTRYPOINT"] == "optimizer"
    system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "Be concise." in system_prompt
    assert "User: first" in system_prompt
    assert "Assistant: seen" in system_prompt


def test_optimizer_assistant_invokes_codex_for_codex_host(
    monkeypatch, tmp_path
) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls = []
    output_path = tmp_path / "last-message.txt"

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        output_path.write_text("codex reply")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(optimizer_assistant, "_temporary_output_path", lambda: output_path)
    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which",
        lambda name: "/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr("claude_smart.optimizer_assistant.subprocess.run", fake_run)
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "/ignored/codex-claude-compat")

    rc, stdout, stderr = _run_main(
        monkeypatch,
        {
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "seen"},
                {"role": "user", "content": "answer now"},
            ],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": "summaries"}],
        },
    )

    assert rc == 0
    assert stderr == ""
    assert json.loads(stdout) == {"content": "codex reply"}
    cmd, kwargs = calls[0]
    assert cmd[:2] == ["/bin/codex", "exec"]
    assert "/ignored/codex-claude-compat" not in cmd
    assert "--output-last-message" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--ask-for-approval" not in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--ephemeral" in cmd
    assert "--ignore-rules" in cmd
    assert kwargs["input"].endswith("## Task\nanswer now")
    assert "Be concise." in kwargs["input"]
    assert "Assistant: seen" in kwargs["input"]
    assert kwargs["env"]["CLAUDE_SMART_HOST"] == "codex"
    assert kwargs["env"]["CLAUDE_SMART_INTERNAL"] == "1"
    assert kwargs["env"]["CLAUDE_CODE_ENTRYPOINT"] == "optimizer"


def test_optimizer_assistant_rejects_invalid_stdin(monkeypatch) -> None:
    rc, stdout, stderr = _run_main(monkeypatch, "{not json")

    assert rc == 1
    assert stdout == ""
    assert "stdin must be a JSON object" in stderr


def test_optimizer_assistant_returns_nonzero_on_claude_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which", lambda _: "claude"
    )
    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.subprocess.run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="boom",
        ),
    )

    rc, stdout, stderr = _run_main(
        monkeypatch,
        {
            "messages": [{"role": "user", "content": "hi"}],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": None}],
        },
    )

    assert rc == 1
    assert stdout == ""
    assert "claude CLI exited 2" in stderr


def test_optimizer_assistant_returns_nonzero_on_malformed_claude_json(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which", lambda _: "claude"
    )
    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.subprocess.run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0,
            stdout="not json",
            stderr="",
        ),
    )

    rc, stdout, stderr = _run_main(
        monkeypatch,
        {
            "messages": [{"role": "user", "content": "hi"}],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": None}],
        },
    )

    assert rc == 1
    assert stdout == ""
    assert "claude CLI returned non-JSON output" in stderr


def test_optimizer_assistant_returns_nonzero_on_timeout(monkeypatch) -> None:
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which", lambda _: "claude"
    )
    monkeypatch.setattr("claude_smart.optimizer_assistant.subprocess.run", fake_run)

    rc, stdout, stderr = _run_main(
        monkeypatch,
        {
            "messages": [{"role": "user", "content": "hi"}],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": None}],
        },
    )

    assert rc == 1
    assert stdout == ""
    assert "claude CLI timed out after 300s" in stderr


def test_optimizer_assistant_prefers_claude_smart_cli_path(monkeypatch) -> None:
    """When ``CLAUDE_SMART_CLI_PATH`` is set, the optimizer assistant must
    invoke the override (e.g. ``opencode-claude-compat``) instead of the real
    ``claude`` binary. backend-service.sh writes this override for the
    OpenCode and Codex hosts so generation routes through the same host-aware
    bridge the reflexio ``claude-code`` provider uses.
    """
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd[0])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": "router reply"}),
            stderr="",
        )

    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which", lambda _: "/bin/claude"
    )
    monkeypatch.setattr("claude_smart.optimizer_assistant.subprocess.run", fake_run)
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "/opt/router")

    rc, stdout, _stderr = _run_main(
        monkeypatch,
        {
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "seen"},
                {"role": "user", "content": "answer now"},
            ],
            "playbooks": [
                {"id": 1, "content": "Be concise.", "trigger": "summaries"}
            ],
        },
    )

    assert rc == 0
    assert json.loads(stdout) == {"content": "router reply"}
    # shutil.which was registered but the override path must be the one
    # actually spawned, because the override is an explicit operator signal.
    assert seen_cmds == ["/opt/router"]
    assert "/bin/claude" not in seen_cmds


def test_optimizer_assistant_opencode_bridge_accepts_optimizer_args(
    monkeypatch, tmp_path
) -> None:
    """Run through the real OpenCode bridge parser so optimizer-only Claude flags
    cannot be reintroduced for host compatibility bridges.
    """
    if os.name == "nt":
        pytest.skip("POSIX bridge wrapper exercised on non-Windows hosts")

    repo_root = Path(__file__).resolve().parents[1]
    bridge = repo_root / "plugin" / "scripts" / "opencode-claude-compat"
    fake_opencode = tmp_path / "opencode"
    fake_opencode.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"type\":\"result\",\"result\":\"bridge reply\"}'\n"
    )
    fake_opencode.chmod(0o755)

    runtime.set_host(runtime.HOST_OPENCODE)
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", str(bridge))
    monkeypatch.setenv("CLAUDE_SMART_OPENCODE_PATH", str(fake_opencode))

    rc, stdout, stderr = _run_main(
        monkeypatch,
        {
            "messages": [
                {"role": "system", "content": "System note."},
                {"role": "user", "content": "hello"},
            ],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": None}],
        },
    )

    assert rc == 0, stderr
    assert stderr == ""
    assert json.loads(stdout) == {"content": "bridge reply"}


def test_optimizer_assistant_override_missing_path_fails_loud(
    monkeypatch, tmp_path
) -> None:
    """An override path that doesn't exist must surface a clear ``FileNotFoundError``
    rather than silently fall back to ``shutil.which("claude")`` (which would
    re-introduce the original CC-quota bug for misconfigured bridges). The
    ``OptimizerAssistantError`` wrapper reports this as
    ``"claude CLI not found on PATH"`` so misconfiguration is diagnosable from
    reflexio's optimizer error surface.
    """
    missing = tmp_path / "does-not-exist.sh"
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", str(missing))

    def fake_run(cmd, **_kwargs):
        raise FileNotFoundError(2, "No such file", str(missing))

    monkeypatch.setattr("claude_smart.optimizer_assistant.subprocess.run", fake_run)

    rc, stdout, stderr = _run_main(
        monkeypatch,
        {
            "messages": [{"role": "user", "content": "hi"}],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": None}],
        },
    )

    assert rc == 1
    assert stdout == ""
    assert "claude CLI not found on PATH" in stderr


def test_optimizer_assistant_falls_back_to_shutil_which_without_override(
    monkeypatch,
) -> None:
    """Without ``CLAUDE_SMART_CLI_PATH``, the legacy Claude Code host path
    continues to use ``shutil.which("claude")`` so non-OpenCode installs keep
    working.
    """
    seen = []

    def fake_run(cmd, **_kwargs):
        seen.append(cmd[0])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which", lambda _: "/usr/bin/claude"
    )
    monkeypatch.setattr("claude_smart.optimizer_assistant.subprocess.run", fake_run)
    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)

    rc, stdout, _stderr = _run_main(
        monkeypatch,
        {
            "messages": [{"role": "user", "content": "hello"}],
            "playbooks": [{"id": 1, "content": "Be concise.", "trigger": None}],
        },
    )

    assert rc == 0
    assert json.loads(stdout) == {"content": "ok"}
    assert seen == ["/usr/bin/claude"]


def test_optimizer_assistant_resolve_helpers(monkeypatch) -> None:
    """The resolver ignores empty overrides and falls through to shutil."""
    monkeypatch.setattr(
        "claude_smart.optimizer_assistant.shutil.which",
        lambda _: "/usr/local/bin/claude",
    )

    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    assert (
        optimizer_assistant._resolve_claude_cli_path() == "/usr/local/bin/claude"
    )

    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "")
    assert (
        optimizer_assistant._resolve_claude_cli_path() == "/usr/local/bin/claude"
    )

    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "/bridge/claude")
    assert (
        optimizer_assistant._resolve_claude_cli_path() == "/bridge/claude"
    )
