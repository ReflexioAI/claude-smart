"""Tests for the Reflexio LocalScriptAssistant bridge."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from claude_smart import optimizer_assistant


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
