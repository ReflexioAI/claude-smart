"""Tests for the forensic hook event log."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_smart import hook, hook_log


@pytest.fixture
def hook_log_path(tmp_path, monkeypatch) -> Path:
    """Redirect ``hook_log`` writes to a per-test tmp path."""
    log_path = tmp_path / "hook.log"
    monkeypatch.setattr(hook_log, "_LOG_PATH", log_path)
    monkeypatch.delenv("CLAUDE_SMART_HOOK_LOG", raising=False)
    return log_path


def _read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


# -----------------------------------------------------------------------------
# hook_log.log_event — direct unit tests
# -----------------------------------------------------------------------------


def test_log_event_writes_one_jsonl_record(hook_log_path) -> None:
    hook_log.log_event(
        event="stop",
        host="claude-code",
        session_id="sess-1",
        project_id="proj-1",
        cwd="/some/dir",
        handler_status="ok",
        publish_status="ok",
        publish_count=2,
    )
    records = _read_lines(hook_log_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "stop"
    assert rec["host"] == "claude-code"
    assert rec["session_id"] == "sess-1"
    assert rec["project_id"] == "proj-1"
    assert rec["cwd"] == "/some/dir"
    assert rec["handler_status"] == "ok"
    assert rec["publish_status"] == "ok"
    assert rec["publish_count"] == 2
    assert rec["internal_skipped"] is False
    assert isinstance(rec["ts"], int)


def test_log_event_appends_across_calls(hook_log_path) -> None:
    hook_log.log_event(event="stop", host="claude-code", session_id="a")
    hook_log.log_event(event="user-prompt", host="claude-code", session_id="b")
    records = _read_lines(hook_log_path)
    assert [r["event"] for r in records] == ["stop", "user-prompt"]


def test_log_event_omits_publish_fields_when_handler_did_not_publish(
    hook_log_path,
) -> None:
    hook_log.log_event(event="user-prompt", host="claude-code", session_id="s")
    rec = _read_lines(hook_log_path)[0]
    assert "publish_status" not in rec
    assert "publish_count" not in rec


def test_log_event_truncates_oversized_strings(hook_log_path) -> None:
    long_cwd = "x" * (hook_log._TRUNCATE_LIMIT + 100)
    hook_log.log_event(event="stop", host="claude-code", cwd=long_cwd)
    rec = _read_lines(hook_log_path)[0]
    # truncation leaves the marker char appended
    assert len(rec["cwd"]) == hook_log._TRUNCATE_LIMIT + 1
    assert rec["cwd"].endswith("…")


def test_log_event_tolerates_readonly_log_dir(tmp_path, monkeypatch) -> None:
    """A read-only parent must not abort the hook fire."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses the read-only bit")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        monkeypatch.setattr(hook_log, "_LOG_PATH", locked / "sub" / "hook.log")
        monkeypatch.delenv("CLAUDE_SMART_HOOK_LOG", raising=False)
        # Must not raise.
        hook_log.log_event(event="stop", host="claude-code")
    finally:
        locked.chmod(0o700)


def test_log_path_honors_env_override(tmp_path, monkeypatch) -> None:
    target = tmp_path / "override.log"
    monkeypatch.setenv("CLAUDE_SMART_HOOK_LOG", str(target))
    hook_log.log_event(event="stop", host="claude-code")
    assert target.is_file()
    assert _read_lines(target)[0]["event"] == "stop"


# -----------------------------------------------------------------------------
# hook.main dispatcher → log records
# -----------------------------------------------------------------------------


@pytest.fixture
def stdin_payload(monkeypatch):
    """Helper that pipes a JSON dict to ``sys.stdin`` for ``hook.main``."""

    def _set(payload: dict[str, Any]) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    return _set


def test_dispatcher_logs_unknown_event(hook_log_path, stdin_payload) -> None:
    stdin_payload({"session_id": "s1", "cwd": "/tmp"})
    hook.main(["claude-code", "made-up-event"])
    rec = _read_lines(hook_log_path)[0]
    assert rec["event"] == "made-up-event"
    assert rec["handler_status"] == "unknown_event"


def test_dispatcher_logs_internal_skipped(
    hook_log_path, stdin_payload, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_INTERNAL", "1")
    stdin_payload({"session_id": "s2", "cwd": "/tmp"})
    hook.main(["claude-code", "stop"])
    rec = _read_lines(hook_log_path)[0]
    assert rec["event"] == "stop"
    assert rec["internal_skipped"] is True
    assert rec["handler_status"] == "ok"
    # The internal-skip path doesn't publish — no publish fields.
    assert "publish_status" not in rec


def test_dispatcher_logs_handler_exception(
    hook_log_path, stdin_payload, monkeypatch
) -> None:
    def boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hook, "_load_handlers", lambda: {"stop": boom})
    stdin_payload({"session_id": "s3", "cwd": "/tmp"})
    hook.main(["claude-code", "stop"])
    rec = _read_lines(hook_log_path)[0]
    assert rec["event"] == "stop"
    assert rec["handler_status"].startswith("raised:RuntimeError:")
    assert "kaboom" in rec["handler_status"]


def test_dispatcher_logs_publish_outcome_from_stop(
    hook_log_path, stdin_payload, monkeypatch
) -> None:
    def stop_ok(_payload: dict[str, Any]) -> tuple[str, int]:
        return ("ok", 3)

    monkeypatch.setattr(hook, "_load_handlers", lambda: {"stop": stop_ok})
    stdin_payload({"session_id": "s4", "cwd": "/tmp"})
    hook.main(["claude-code", "stop"])
    rec = _read_lines(hook_log_path)[0]
    assert rec["event"] == "stop"
    assert rec["handler_status"] == "ok"
    assert rec["publish_status"] == "ok"
    assert rec["publish_count"] == 3


def test_dispatcher_logs_nothing_publish_status(
    hook_log_path, stdin_payload, monkeypatch
) -> None:
    def stop_nothing(_payload: dict[str, Any]) -> tuple[str, int]:
        return ("nothing", 0)

    monkeypatch.setattr(hook, "_load_handlers", lambda: {"stop": stop_nothing})
    stdin_payload({"session_id": "s5", "cwd": "/tmp"})
    hook.main(["claude-code", "stop"])
    rec = _read_lines(hook_log_path)[0]
    assert rec["publish_status"] == "nothing"
    assert rec["publish_count"] == 0
