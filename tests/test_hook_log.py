"""Tests for the forensic hook event log."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
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
    return log_path


def _read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


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
        # Must not raise.
        hook_log.log_event(event="stop", host="claude-code")
    finally:
        locked.chmod(0o700)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
@pytest.mark.parametrize("filename", ["hook.log", "hook.log.lock", "hook.log.rot"])
def test_hook_does_not_block_on_session_diagnostic_fifo(tmp_path, filename) -> None:
    state_dir = tmp_path / "sessions"
    state_dir.mkdir()
    os.mkfifo(state_dir / filename)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "plugin" / "src"),
        "CLAUDE_SMART_STATE_DIR": str(state_dir),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from claude_smart import hook, hook_log; hook_log._MAX_LOG_BYTES = 1; "
                "raise SystemExit(hook.main(['opencode', 'test-observation']))"
            ),
        ],
        cwd=tmp_path,
        env=env,
        input=json.dumps({"session_id": "test", "cwd": str(tmp_path)}),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["continue"] is True
    if filename.endswith(".lock"):
        assert not (state_dir / "hook.log").exists()
    if filename.endswith(".rot"):
        assert _read_lines(state_dir / "hook.log")[0]["session_id"] == "test"
        assert not list(state_dir.glob(".hook-log-*"))


def test_log_path_follows_the_session_directory(
    hook_log_path, tmp_path, monkeypatch
) -> None:
    state_dir = tmp_path / "sessions"
    previous_log = tmp_path / "previous-hook.log"
    previous_log.write_text("old log stays in place")
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CLAUDE_SMART_HOOK_LOG", str(previous_log))
    hook_log.log_event(event="stop", host="claude-code")
    assert _read_lines(state_dir / "hook.log")[0]["event"] == "stop"
    assert not hook_log_path.exists()
    assert previous_log.read_text() == "old log stays in place"


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


@pytest.mark.parametrize("cwd_key", ["workingDirectory", "currentWorkingDirectory"])
def test_dispatcher_normalizes_desktop_payload_keys(
    hook_log_path, stdin_payload, monkeypatch, cwd_key: str
) -> None:
    seen: dict[str, Any] = {}

    def stop_ok(payload: dict[str, Any]) -> tuple[str, int]:
        seen.update(payload)
        return ("ok", 1)

    monkeypatch.setattr(hook, "_load_handlers", lambda: {"stop": stop_ok})
    stdin_payload(
        {
            "sessionId": "s_desktop",
            "transcriptPath": "/tmp/transcript.jsonl",
            "toolName": "Bash",
            "toolInput": {"command": "pytest"},
            "toolResponse": {"ok": True},
            "lastAssistantMessage": "done",
            cwd_key: "/tmp/project",
        }
    )
    hook.main(["claude-code", "stop"])

    assert seen["session_id"] == "s_desktop"
    assert seen["transcript_path"] == "/tmp/transcript.jsonl"
    assert seen["tool_name"] == "Bash"
    assert seen["tool_input"] == {"command": "pytest"}
    assert seen["tool_response"] == {"ok": True}
    assert seen["last_assistant_message"] == "done"
    assert seen["cwd"] == "/tmp/project"
    rec = _read_lines(hook_log_path)[0]
    assert rec["session_id"] == "s_desktop"


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


def test_dispatcher_does_not_append_continue_after_handler_json(
    hook_log_path, stdin_payload, monkeypatch, capsys
) -> None:
    """Context-injection handlers already emit hook JSON; don't append another."""

    def inject_context(_payload: dict[str, Any]) -> None:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": "matched context",
                    }
                }
            )
        )
        sys.stdout.write("\n")

    monkeypatch.setattr(hook, "_load_handlers", lambda: {"user-prompt": inject_context})
    stdin_payload({"session_id": "s-inject", "cwd": "/tmp"})
    hook.main(["claude-code", "user-prompt"])
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines == [
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "matched context",
            }
        }
    ]


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink traversal")
def test_rotating_log_uses_the_physical_session_directory(
    tmp_path, monkeypatch
) -> None:
    physical = tmp_path / "physical"
    nested = physical / "nested"
    nested.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(nested, target_is_directory=True)
    state_dir = link / ".." / "sessions"
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(state_dir))
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 1)

    hook_log.log_event(event="stop", host="claude-code", session_id="first")
    hook_log.log_event(event="stop", host="claude-code", session_id="second")

    log_path = physical / "sessions" / "hook.log"
    assert [row["session_id"] for row in _read_lines(log_path)] == ["second"]
    assert not (tmp_path / "sessions").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink traversal")
@pytest.mark.parametrize("make_loop", [False, True])
def test_rotation_keeps_the_log_in_the_directory_it_locked(
    tmp_path, monkeypatch, make_loop
) -> None:
    first = tmp_path / "first"
    other = tmp_path / "other"
    first.mkdir()
    other.mkdir()
    other_log = other / "hook.log"
    other_log.write_bytes(b'{"session_id":"other"}\n')
    link = tmp_path / "link"
    link.symlink_to(first, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(link))
    hook_log.log_event(event="stop", host="claude-code", session_id="first")
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 1)
    replace = os.replace

    def replace_link_then_rotate(source, target, **kwargs):
        link.unlink()
        link.symlink_to(link if make_loop else other, target_is_directory=True)
        return replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", replace_link_then_rotate)
    hook_log.log_event(event="stop", host="claude-code", session_id="second")

    assert [row["session_id"] for row in _read_lines(first / "hook.log")] == ["second"]
    assert other_log.read_text() == '{"session_id":"other"}\n'


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory rename")
def test_rotation_does_not_overwrite_replacement_session_directory(
    tmp_path, monkeypatch
) -> None:
    active = tmp_path / "active"
    archive = tmp_path / "archive"
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(active))
    hook_log.log_event(event="stop", host="claude-code", session_id="first")
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 1)
    replace = os.replace
    fresh = b'{"session_id":"fresh"}\n'
    rotated = False

    def archive_then_replace(source, target, **kwargs):
        nonlocal rotated
        if Path(target).name == "hook.log" and not rotated:
            rotated = True
            active.rename(archive)
            active.mkdir()
            (active / "hook.log").write_bytes(fresh)
        return replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", archive_then_replace)
    hook_log.log_event(event="stop", host="claude-code", session_id="second")

    assert rotated
    assert (active / "hook.log").read_bytes() == fresh
    assert [row["session_id"] for row in _read_lines(archive / "hook.log")] == [
        "second"
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_rotating_log_retains_existing_access(hook_log_path, monkeypatch) -> None:
    hook_log.log_event(event="stop", host="claude-code", session_id="first")
    hook_log_path.chmod(0o640)
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 1)
    hook_log.log_event(event="stop", host="claude-code", session_id="second")
    assert [row["session_id"] for row in _read_lines(hook_log_path)] == ["second"]
    assert stat.S_IMODE(hook_log_path.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("operation", ["fchown", "fchmod"])
def test_rotation_stays_bounded_when_access_cannot_be_copied(
    hook_log_path, monkeypatch, operation
) -> None:
    hook_log.log_event(event="stop", host="claude-code", session_id="first")
    hook_log_path.chmod(0o640)

    def denied(*_args):
        raise PermissionError("cannot copy access")

    if operation == "fchown":
        fstat = os.fstat

        def different_group(fd):
            info = list(fstat(fd))
            info[stat.ST_GID] += 1
            return os.stat_result(info)

        monkeypatch.setattr(os, "fstat", different_group)
    monkeypatch.setattr(os, operation, denied)
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 1)
    hook_log.log_event(event="stop", host="claude-code", session_id="second")
    assert [row["session_id"] for row in _read_lines(hook_log_path)] == ["second"]
    assert stat.S_IMODE(hook_log_path.stat().st_mode) == 0o600


def test_log_does_not_rotate_when_under_max_bytes(hook_log_path, monkeypatch) -> None:
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


def test_log_rotation_preserves_jsonl_framing(hook_log_path, monkeypatch) -> None:
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


def test_log_rotation_tolerates_read_failure(hook_log_path, monkeypatch) -> None:
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
def test_contended_log_skips_append_without_blocking_or_racing_rotation(
    hook_log_path, monkeypatch
) -> None:
    """Best-effort logging cannot block a hook behind another log rotation.

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
    B must return while A still holds the lock, without modifying its snapshot.
    A later uncontended append must succeed against the rotated file.
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

    # Pause ONLY the first ``os.replace`` call on the log file — that's
    # the rotator's. The appender is either blocked by the lock (good)
    # or runs concurrently and never reaches ``replace`` itself (we let
    # it finish a single append; rotation can happen freely afterwards).
    replace_called = threading.Event()
    replace_may_finish = threading.Event()
    appender_finished = threading.Event()
    call_count = {"n": 0}
    real_replace = os.replace

    def paused_replace(source, target, **kwargs) -> None:
        if Path(target).name == hook_log_path.name:
            call_count["n"] += 1
            if call_count["n"] == 1:
                replace_called.set()
                assert replace_may_finish.wait(timeout=5.0)
        real_replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", paused_replace)

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

    # The held rotation must not delay B; its diagnostic record is optional.
    assert replace_called.wait(timeout=5.0)
    try:
        assert appender_finished.wait(timeout=0.5), (
            "contended diagnostic blocked the hook"
        )
        assert "appender-record" not in {
            r["session_id"] for r in _read_lines(hook_log_path)
        }
    finally:
        replace_may_finish.set()
    t_rot.join(timeout=5.0)
    t_app.join(timeout=5.0)
    assert not t_rot.is_alive()
    assert not t_app.is_alive()

    final = _read_lines(hook_log_path)
    session_ids = {r["session_id"] for r in final}
    assert "rotator-record" in session_ids
    assert "appender-record" not in session_ids
    hook_log.log_event(event="stop", host="codex", session_id="after-contention")
    assert "after-contention" in {r["session_id"] for r in _read_lines(hook_log_path)}


def test_handler_completion_records_elapsed_stage_metadata(
    hook_log_path, stdin_payload, monkeypatch
) -> None:
    monkeypatch.setattr(
        hook, "_load_handlers", lambda: {"post-tool": lambda payload: None}
    )
    stdin_payload({"session_id": "timing", "cwd": "/tmp"})
    assert hook.main(["codex", "post-tool"]) == 0
    [record] = _read_lines(hook_log_path)
    assert record["stage"] == "handler"
    assert record["duration_ms"] >= 0


@pytest.mark.parametrize("manifest_failure", ["missing", "malformed"])
def test_hook_identity_comes_from_executing_runtime(
    hook_log_path, tmp_path, monkeypatch, manifest_failure
) -> None:
    import runpy
    import shutil

    from claude_smart import runtime

    executing = tmp_path / "old-runtime"
    installed = tmp_path / "new-runtime"
    for root, version in [(executing, "1.0.0"), (installed, "2.0.0")]:
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"version": version})
        )
    source = executing / "src" / "claude_smart" / "runtime.py"
    source.parent.mkdir(parents=True)
    shutil.copy2(runtime.__file__, source)
    monkeypatch.setenv("CLAUDE_SMART_HOST", "codex")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(installed))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "account"))
    loaded = runpy.run_path(str(source))
    monkeypatch.setattr(runtime, "plugin_metadata", loaded["plugin_metadata"])
    # The loaded runtime stays at v1 even if its files are replaced mid-hook.
    (executing / ".codex-plugin" / "plugin.json").write_text('{"version":"3.0.0"}')
    hook_log.log_event(event="stop", host="codex", session_id="existing")
    record = _read_lines(hook_log_path)[0]
    assert record["plugin_version"] == "1.0.0"
    assert record["plugin_root"] == str(executing)
    assert record["codex_home"] == str(tmp_path / "account")
    manifest = executing / ".codex-plugin" / "plugin.json"
    if manifest_failure == "missing":
        manifest.unlink()
    else:
        manifest.write_text("invalid json")
    loaded = runpy.run_path(str(source))
    monkeypatch.setattr(runtime, "plugin_metadata", loaded["plugin_metadata"])
    hook_log.log_event(event="stop", host="codex", session_id="existing")
    assert _read_lines(hook_log_path)[-1]["plugin_version"] == "unknown"


@pytest.mark.parametrize("resolution_error", [OSError, RuntimeError])
def test_hook_logging_omits_account_when_resolution_fails(
    hook_log_path, tmp_path, monkeypatch, resolution_error
) -> None:
    from claude_smart import runtime

    account = tmp_path / "account"
    monkeypatch.setenv("CODEX_HOME", str(account))
    runtime.set_host("codex")
    identity = runtime.plugin_metadata()
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == account:
            raise resolution_error("account path unavailable")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    hook_log.log_event(event="stop", host="codex", session_id="existing")
    record = _read_lines(hook_log_path)[0]
    assert "codex_home" not in record
    assert record["plugin_version"] == identity["plugin_version"]
    assert record["plugin_root"] == identity["plugin_root"]


def test_codex_hook_continues_with_account_symlink_loop(
    hook_log_path, tmp_path, stdin_payload, monkeypatch, capsys
) -> None:
    account = tmp_path / "account"
    try:
        account.symlink_to(account)
    except OSError:
        pytest.skip("symlink creation unavailable")
    monkeypatch.setenv("CODEX_HOME", str(account))
    monkeypatch.setattr(hook, "_load_handlers", lambda: {"stop": lambda _: None})
    stdin_payload({"session_id": "existing", "cwd": str(tmp_path)})

    assert hook.main(["codex", "stop"]) == 0
    assert json.loads(capsys.readouterr().out) == {"continue": True}
    record = _read_lines(hook_log_path)[0]
    assert record["handler_status"] == "ok"
    assert record["session_id"] == "existing"
    assert "codex_home" not in record
    assert record["plugin_version"]
    assert record["plugin_root"]


def test_rotation_retains_latest_record_larger_than_cap(hook_log_path, monkeypatch):
    monkeypatch.setattr(hook_log, "_MAX_LOG_BYTES", 64)
    hook_log.log_event(event="stop", host="claude-code", session_id="older")
    hook_log.log_event(event="stop", host="claude-code", session_id="newest")
    assert [row["session_id"] for row in _read_lines(hook_log_path)] == ["newest"]


def test_test_runs_never_write_the_account_hook_log(tmp_path, monkeypatch) -> None:
    """Suites that import the plugin in-process must not pollute the real log.

    An external integration suite published through the adapter without this
    repository's fixtures and wrote fake ``publish-result`` events into the
    user's ``~/.claude-smart/hook.log``.
    """
    account_log = tmp_path / "account-home" / ".claude-smart" / "hook.log"
    monkeypatch.setattr(hook_log, "_account_log_path", lambda: account_log)
    monkeypatch.setattr(hook_log, "_LOG_PATH", account_log)
    assert os.environ.get("PYTEST_CURRENT_TEST")

    hook_log.log_event(event="publish-result", host="codex", session_id="session")
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(account_log.parent))
    hook_log.log_event(event="publish-result", host="codex", session_id="session")
    assert not account_log.exists()

    # An isolated session directory still records normally.
    redirected = tmp_path / "sessions"
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(redirected))
    hook_log.log_event(event="stop", host="codex", session_id="session")
    assert _read_lines(redirected / "hook.log")[0]["event"] == "stop"
