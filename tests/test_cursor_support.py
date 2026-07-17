"""Regression coverage for Cursor host attribution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from claude_smart import hook, hook_log, runtime, state


@pytest.fixture(autouse=True)
def _clear_cursor_detection_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Cursor host signals local to each test."""
    for name in (
        "CLAUDE_CODE_ENTRYPOINT",
        "CURSOR_PROJECT_DIR",
        "CURSOR_TRANSCRIPT_PATH",
        "CURSOR_USER_EMAIL",
        "CURSOR_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cursor_hook_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect hook telemetry away from the user's real state."""
    path = tmp_path / "hook.log"
    monkeypatch.setattr(hook_log, "_LOG_PATH", path)
    monkeypatch.delenv("CLAUDE_SMART_HOOK_LOG", raising=False)
    return path


def _log_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_cursor_hook_records_cursor_in_session_and_hook_log(
    session_dir: Path,
    cursor_hook_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Cursor-fired Claude Code plugin hook keeps Cursor provenance."""
    monkeypatch.setenv("CURSOR_VERSION", "3.11.0")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(
        hook,
        "_read_stdin_json",
        lambda: {
            "session_id": "cursor-session",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/example.py"},
            "tool_response": {"ok": True},
        },
    )

    assert hook.main(["claude-code", "post-tool"]) == 0

    assert state.read_all("cursor-session")[0]["host"] == "cursor"
    log_record = _log_records(cursor_hook_log)[0]
    assert log_record["host"] == "cursor"
    assert log_record["cwd"] == str(tmp_path)
    assert log_record["project_id"] == tmp_path.name


def test_claude_code_entrypoint_wins_over_cursor_environment(
    session_dir: Path,
    cursor_hook_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code launched inside Cursor stays attributed to Claude Code."""
    monkeypatch.setenv("CURSOR_VERSION", "3.11.0")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setattr(
        hook,
        "_read_stdin_json",
        lambda: {
            "session_id": "claude-session",
            "cwd": str(tmp_path),
            "tool_name": "Read",
            "tool_response": {"ok": True},
        },
    )

    assert hook.main(["claude-code", "post-tool"]) == 0

    assert state.read_all("claude-session")[0]["host"] == "claude-code"
    assert _log_records(cursor_hook_log)[0]["host"] == "claude-code"


@pytest.mark.parametrize(
    ("env", "payload", "expected"),
    [
        ({"CURSOR_TRANSCRIPT_PATH": "/tmp/cursor.jsonl"}, {}, "cursor"),
        (
            {"CURSOR_VERSION": "3.11", "CURSOR_PROJECT_DIR": "/tmp/project"},
            {},
            "cursor",
        ),
        ({"CURSOR_VERSION": "3.11"}, {}, "claude-code"),
        (
            {
                "CURSOR_TRANSCRIPT_PATH": "/tmp/cursor.jsonl",
                "CLAUDE_CODE_ENTRYPOINT": "cli",
            },
            {},
            "claude-code",
        ),
        (
            {},
            {
                "transcript_path": str(
                    Path.home() / ".cursor" / "projects" / "session.jsonl"
                )
            },
            "cursor",
        ),
        (
            {},
            {
                "transcript_path": str(
                    Path.home() / ".claude" / "projects" / "session.jsonl"
                )
            },
            "claude-code",
        ),
    ],
)
def test_resolve_hook_host_uses_layered_cursor_signals(
    env: dict[str, str],
    payload: dict[str, str],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert runtime.resolve_hook_host("claude-code", payload) == expected


def test_explicit_non_claude_host_is_not_reclassified_as_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_TRANSCRIPT_PATH", "/tmp/cursor.jsonl")

    assert runtime.resolve_hook_host("opencode", {}) == "opencode"


def test_cursor_shares_learnings_without_losing_attribution() -> None:
    assert runtime.set_host("cursor") == "cursor"
    assert runtime.host() == "cursor"
    assert runtime.attribution_host() == "cursor"
    assert runtime.agent_version() == "claude-code"
