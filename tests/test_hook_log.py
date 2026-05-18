"""Tests for the forensic hook event log."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
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
    if not hasattr(os, "geteuid"):
        pytest.skip("read-only permission semantics test is POSIX-specific")
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


def test_dispatcher_emits_continue_on_handler_success(
    hook_log_path, stdin_payload, monkeypatch, capsys
) -> None:
    """The success path must emit the ``{"continue": true}`` JSON on stdout.

    Regression: an earlier version only emitted on the exception path,
    leaving successful hook fires silent and blocking the parent session.
    """

    def stop_ok(_payload: dict[str, Any]) -> tuple[str, int]:
        return ("ok", 1)

    monkeypatch.setattr(hook, "_load_handlers", lambda: {"stop": stop_ok})
    stdin_payload({"session_id": "s-success", "cwd": "/tmp"})
    hook.main(["claude-code", "stop"])
    out = capsys.readouterr().out
    assert '"continue": true' in out


def test_dispatcher_emits_continue_on_handler_exception(
    hook_log_path, stdin_payload, monkeypatch, capsys
) -> None:
    """The exception path must also emit ``{"continue": true}`` — the
    parent session never blocks regardless of handler outcome."""

    def boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "_load_handlers", lambda: {"stop": boom})
    stdin_payload({"session_id": "s-exc", "cwd": "/tmp"})
    hook.main(["claude-code", "stop"])
    out = capsys.readouterr().out
    assert '"continue": true' in out


def test_log_event_tolerates_unparseable_publish_count(hook_log_path) -> None:
    """A non-int publish_count must not raise — the hook is best-effort.

    Addresses CodeRabbit review on PR #30: ``int(publish_count or 0)``
    used to raise TypeError on garbage callers, breaking the "never abort
    a hook fire" contract. Defensive coercion falls back to 0.
    """
    # Object value with no usable int conversion.
    hook_log.log_event(
        event="stop",
        host="claude-code",
        publish_status="ok",
        publish_count=object(),  # type: ignore[arg-type]
    )
    rec = _read_lines(hook_log_path)[0]
    assert rec["publish_status"] == "ok"
    assert rec["publish_count"] == 0  # fell back to default

    # And a string that can't be parsed as int.
    hook_log.log_event(
        event="stop",
        host="claude-code",
        publish_status="ok",
        publish_count="not-a-number",  # type: ignore[arg-type]
    )
    rec = _read_lines(hook_log_path)[1]
    assert rec["publish_count"] == 0


# -----------------------------------------------------------------------------
# hook_log — bounded rotation
# -----------------------------------------------------------------------------


def test_log_rotates_when_over_max_bytes(hook_log_path, monkeypatch) -> None:
    """When the file crosses the cap, the oldest half is dropped and the
    most-recent records survive."""
    # Shrink the cap to make rotation testable without writing 5 MB.
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 500)

    # Each record below is well under 500 B, but ~30 of them will cross
    # the cap and trigger at least one rotation.
    for i in range(60):
        hook_log.log_event(
            event="stop",
            host="claude-code",
            session_id=f"sess-{i:04d}",
        )

    # Total size must stay roughly bounded by the cap. The "roughly"
    # allowance covers the single record that pushes us over before the
    # next rotation reclaims half. In practice <= cap + one record.
    final_size = hook_log_path.stat().st_size
    assert final_size <= hook_log._MAX_LOG_BYTES * 2, (
        f"expected size <= {hook_log._MAX_LOG_BYTES * 2}, got {final_size}"
    )

    # The most-recent record must still be present — rotation drops the
    # oldest half, never the tail.
    records = _read_lines(hook_log_path)
    assert records, "rotation must not empty the file"
    session_ids = [r["session_id"] for r in records]
    assert "sess-0059" in session_ids
    # And the very oldest must be gone.
    assert "sess-0000" not in session_ids


def test_log_does_not_rotate_when_under_max_bytes(
    hook_log_path, monkeypatch
) -> None:
    """A single small record never triggers rotation; bytes are byte-stable
    across subsequent reads."""
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 5 * 1024 * 1024)

    hook_log.log_event(event="stop", host="claude-code", session_id="only")
    snapshot_bytes = hook_log_path.read_bytes()
    snapshot_records = _read_lines(hook_log_path)

    # A subsequent rotation check on the same under-cap file must be a
    # no-op — the bytes on disk are identical.
    hook_log._maybe_rotate(hook_log_path)
    assert hook_log_path.read_bytes() == snapshot_bytes
    assert _read_lines(hook_log_path) == snapshot_records


def test_log_rotation_preserves_jsonl_framing(
    hook_log_path, monkeypatch
) -> None:
    """After rotation, every surviving line parses as JSON — no record is
    split mid-byte by the cut."""
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 400)

    for i in range(50):
        hook_log.log_event(
            event="user-prompt",
            host="claude-code",
            session_id=f"s-{i:03d}",
        )

    raw = hook_log_path.read_text(encoding="utf-8")
    lines = [line for line in raw.split("\n") if line]
    # Every retained line must be valid JSON — this catches the
    # split-record regression the midpoint-then-newline cut prevents.
    for line in lines:
        parsed = json.loads(line)
        assert "event" in parsed
        assert parsed["event"] == "user-prompt"


def test_log_rotation_tolerates_read_failure(
    hook_log_path, monkeypatch
) -> None:
    """If the rotation read fails (disk error, EACCES, etc.) ``log_event``
    must still return cleanly — hook fires are never aborted by logging."""
    # Prime the file at the default (large) cap so the prime write itself
    # never rotates — we want rotation to fire on the *next* call, where
    # we'll sabotage it.
    hook_log.log_event(event="stop", host="claude-code", session_id="prime")
    assert hook_log_path.stat().st_size > 0

    # Shrink the cap so the next write definitely triggers rotation.
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 1)

    # Sabotage the rotation read step.
    def boom(self: Path) -> bytes:
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_bytes", boom)

    # Must not raise. The append succeeds; rotation's read fails and is
    # swallowed; the caller sees a clean return.
    hook_log.log_event(event="stop", host="claude-code", session_id="post")


@pytest.mark.skipif(
    not hasattr(hook_log, "_fcntl") or hook_log._fcntl is None,
    reason="flock-based rotation lock is POSIX-only",
)
def test_log_event_lock_serialises_rotation_and_append(
    hook_log_path, monkeypatch
) -> None:
    """A concurrent append fired mid-rotation must not be silently dropped.

    Regression for CodeRabbit review on PR #30: the rotation
    ``read_bytes → write tmp → rename`` sequence used to race with a
    concurrent appender — a record landing in the original file between
    the read and the rename was overwritten by the rename and lost. The
    exclusive flock around append + rotate closes that gap.

    Forces the race deterministically: thread A's ``log_event`` triggers
    rotation and is paused inside ``Path.replace``; thread B's
    ``log_event`` fires while A is paused. Without the lock, B would
    append to the original file and A's pending replace would then
    overwrite that append with the pre-B snapshot — B's record is lost.
    With the lock, B blocks on the flock until A releases, then writes
    to the post-rotation file. The test asserts both records survive.
    """
    # Prime with a high cap so no rotation fires during priming.
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 100_000)
    for i in range(8):
        hook_log.log_event(
            event="stop", host="claude-code", session_id=f"prime-{i:02d}"
        )
    primed_size = hook_log_path.stat().st_size
    assert primed_size > 0

    # Drop the cap below the primed size so the *next* log_event rotates.
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", primed_size - 1)

    # Pause ONLY the first ``Path.replace`` call on the log file — that's
    # the rotator's. The appender is either blocked by the lock (good)
    # or runs concurrently and never reaches ``replace`` itself (we let
    # it finish a single append; rotation can happen freely afterwards).
    replace_called = threading.Event()
    replace_may_finish = threading.Event()
    appender_finished = threading.Event()
    call_count = {"n": 0}
    real_replace = Path.replace

    def paused_replace(self: Path, target: Any) -> Path:
        if Path(target) == hook_log_path:
            call_count["n"] += 1
            if call_count["n"] == 1:
                replace_called.set()
                assert replace_may_finish.wait(timeout=5.0)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", paused_replace)

    def rotator() -> None:
        hook_log.log_event(
            event="stop", host="claude-code", session_id="rotator-record"
        )

    def appender() -> None:
        assert replace_called.wait(timeout=5.0)
        hook_log.log_event(
            event="stop", host="claude-code", session_id="appender-record"
        )
        appender_finished.set()

    t_rot = threading.Thread(target=rotator, name="rotator")
    t_app = threading.Thread(target=appender, name="appender")
    t_rot.start()
    t_app.start()

    # Rotator is paused at replace. With the lock held, the appender's
    # log_event call is blocked acquiring the flock — give it generous
    # wall time to attempt and queue.
    assert replace_called.wait(timeout=5.0)
    completed_early = appender_finished.wait(timeout=0.5)
    assert not completed_early, (
        "appender finished before rotator released the lock — the lock "
        "isn't actually serialising rotation against concurrent appends, "
        "so a record could be silently dropped under real concurrency."
    )

    replace_may_finish.set()
    t_rot.join(timeout=5.0)
    t_app.join(timeout=5.0)
    assert not t_rot.is_alive()
    assert not t_app.is_alive()

    final = _read_lines(hook_log_path)
    session_ids = {r["session_id"] for r in final}
    assert "appender-record" in session_ids, (
        "appender's record was dropped by rotation — the lock did not "
        f"serialise the rotation/append. File has: {sorted(session_ids)}"
    )
