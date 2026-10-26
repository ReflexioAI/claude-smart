"""Durability, destination isolation and bounded worker ownership contracts."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from unittest.mock import Mock

import pytest
from filelock import FileLock

from claude_smart import env_config, publish, publish_outbox as outbox, runtime, state
from claude_smart.events import stop
from claude_smart.reflexio_adapter import PublishResult

_REAL_SPAWN = outbox._spawn_worker


def _worker():
    claim = outbox._root() / "launch.claim"
    token = json.loads(claim.read_text())["token"] if claim.exists() else None
    outbox.run_worker(token)


@pytest.fixture
def adapter(monkeypatch, session_dir):
    client = Mock(url="https://backend.example/", api_key="private-credential")
    client.publish.side_effect = lambda **kw: PublishResult(True, kw["request_id"])
    monkeypatch.setattr(outbox, "Adapter", lambda: client)
    return client


def _turn(session_id="s1", text="answer"):
    state.append(session_id, {"role": "User", "content": "question"})
    state.reserve_publish_request_id(session_id)
    state.append(session_id, {"role": "Assistant", "content": text})


def _jobs(session_dir):
    return list((session_dir / "publish-outbox").glob("*.json"))


def test_enqueue_durable_private_metadata_survives_spawn_failure(
    adapter, session_dir, monkeypatch
):
    _turn()
    monkeypatch.setattr(
        outbox, "_spawn_worker", Mock(side_effect=OSError("spawn failed"))
    )
    assert outbox.enqueue("s1", "project")
    [path] = _jobs(session_dir)
    assert not (path.parent / "launch.claim").exists()
    text = path.read_text()
    assert "private-credential" not in text and "backend.example" not in text
    assert "question" not in text and "answer" not in text
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert state.published_record_offset(state.read_all("s1")) == 0
    adapter.publish.assert_not_called()
    _worker()
    assert not _jobs(session_dir)
    assert adapter.publish.call_count == 1
    [event] = [
        json.loads(line)
        for line in (session_dir / "hook.log").read_text().splitlines()
        if json.loads(line)["event"] == "publish-worker"
    ]
    assert event["stage"] == "publication"
    assert event["duration_ms"] >= 0
    assert event["publish_status"] == "ok" and event["publish_count"] == 2
    assert "private-credential" not in json.dumps(event)
    assert "answer" not in json.dumps(event)


def test_failed_then_recovered_publish_keeps_exact_request_id(adapter, session_dir):
    _turn()
    adapter.publish.side_effect = [PublishResult(False), PublishResult(True, "ack")]
    assert outbox.enqueue("s1", "project")
    _worker()
    assert len(_jobs(session_dir)) == 1
    assert state.published_record_offset(state.read_all("s1")) == 0
    _worker()
    first, second = adapter.publish.call_args_list
    assert first.kwargs == second.kwargs
    assert not _jobs(session_dir)


def test_worker_never_publishes_prompt_after_the_enqueued_stop(adapter, session_dir):
    _turn()
    assert outbox.enqueue("s1", "project")
    state.append("s1", {"role": "User", "content": "future unfinished question"})
    state.reserve_publish_request_id("s1")
    _worker()
    assert adapter.publish.call_count == 1
    assert [
        turn["content"] for turn in adapter.publish.call_args.kwargs["interactions"]
    ] == ["question", "answer"]
    assert not _jobs(session_dir)
    assert (
        state.unpublished_slice(state.read_all("s1"))[1][0]["content"]
        == "future unfinished question"
    )


def test_manual_drain_past_queued_bound_retires_stale_intent_and_other_job_progresses(
    adapter, session_dir
):
    _turn("s1")
    assert outbox.enqueue("s1", "project")
    _turn("s1", "manually published newer answer")
    status, count = publish.publish_unpublished(
        session_id="s1",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    assert status == "ok" and count == 4
    _turn("s2")
    assert outbox.enqueue("s2", "project")

    _worker()

    assert not _jobs(session_dir)
    assert [call.kwargs["session_id"] for call in adapter.publish.call_args_list] == [
        "s1",
        "s1",
        "s2",
    ]


def test_crash_releases_owner_without_retiring_intent(adapter, session_dir):
    _turn()
    adapter.publish.side_effect = RuntimeError("crash after intent")
    assert outbox.enqueue("s1", "project")
    with pytest.raises(RuntimeError, match="crash"):
        _worker()
    assert len(_jobs(session_dir)) == 1
    adapter.publish.side_effect = lambda **kw: PublishResult(True, kw["request_id"])
    _worker()
    assert not _jobs(session_dir)


def test_enqueue_during_publish_cannot_be_deleted_by_prior_generation(
    adapter, session_dir
):
    _turn()
    assert outbox.enqueue("s1", "project")

    def complete(**kw):
        if adapter.publish.call_count == 1:
            _turn(text="second answer")
            assert outbox.enqueue("s1", "project")
        return PublishResult(True, kw["request_id"])

    adapter.publish.side_effect = complete
    _worker()
    assert adapter.publish.call_count == 2
    assert not _jobs(session_dir)
    assert [
        call.kwargs["interactions"][-1]["content"]
        for call in adapter.publish.call_args_list
    ] == ["answer", "second answer"]


def test_owner_exclusion_and_healthy_continuation(adapter, session_dir, monkeypatch):
    for session in ("s1", "s2"):
        _turn(session)
        assert outbox.enqueue(session, "project")
    with FileLock(session_dir / "publish-outbox" / "worker.lock", timeout=0):
        _worker()
    adapter.publish.assert_not_called()
    spawn = Mock()
    monkeypatch.setattr(outbox, "_spawn_worker", spawn)
    monkeypatch.setattr(outbox, "_MAX_BATCHES", 1)
    _worker()
    assert adapter.publish.call_count == 1
    assert len(_jobs(session_dir)) == 1
    assert spawn.call_count == 1
    _worker()
    assert not _jobs(session_dir)


def test_worker_exit_and_enqueue_share_admission_gate(
    adapter, session_dir, monkeypatch
):
    _turn()
    assert outbox.enqueue("s1", "project")
    selected = outbox._select
    started = threading.Event()
    attempted = threading.Event()
    spawned = Mock()
    monkeypatch.setattr(outbox, "_spawn_worker", spawned)
    result = []

    def producer():
        assert started.wait(2)
        _turn("s2")
        attempted.set()
        result.append(outbox.enqueue("s2", "project"))

    thread = threading.Thread(target=producer)
    thread.start()

    def select(*args):
        value = selected(*args)
        if value is None and not started.is_set():
            started.set()
            assert attempted.wait(2)
        return value

    monkeypatch.setattr(outbox, "_select", select)
    _worker()
    thread.join(2)
    assert not thread.is_alive() and result == [True]
    # Either the original worker sees s2, or the producer sees a released
    # owner and starts its successor. The durable request cannot be orphaned.
    assert adapter.publish.call_count == 2 or spawned.call_count == 1


@pytest.mark.parametrize("change", ["url", "api_key"])
def test_destination_changes_hold_pending_queue_without_rebinding(
    adapter, session_dir, change
):
    _turn()
    assert outbox.enqueue("s1", "project")
    [path] = _jobs(session_dir)
    original = path.read_bytes()
    setattr(adapter, change, "changed-destination")
    assert not outbox.enqueue("s1", "project")
    _worker()
    adapter.publish.assert_not_called()
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "contents", ['CLAUDE_SMART_READ_ONLY="1"\n', 'REFLEXIO_API_KEY="rotated"\n']
)
def test_fresh_environment_change_blocks_inherited_worker(
    adapter, session_dir, contents
):
    _turn()
    assert outbox.enqueue("s1", "project")
    env_config.CLAUDE_SMART_ENV_PATH.parent.mkdir(parents=True)
    env_config.CLAUDE_SMART_ENV_PATH.write_text(contents)
    _worker()
    adapter.publish.assert_not_called()
    assert len(_jobs(session_dir)) == 1


def test_manual_and_background_publishers_share_session_drain_lock(
    adapter, session_dir
):
    _turn()
    with FileLock(str(state.session_path("s1")) + ".publish.lock", timeout=0):
        assert publish.publish_unpublished(
            session_id="s1",
            project_id="project",
            force_extraction=False,
            skip_aggregation=False,
            adapter=adapter,
        ) == ("failed", 0)
    adapter.publish.assert_not_called()


def test_codex_stop_queues_without_transcript_read_or_http(
    adapter, session_dir, monkeypatch
):
    runtime.set_host(runtime.HOST_CODEX)
    monkeypatch.setattr(stop.ids, "resolve_user_id", lambda _: "project")
    monkeypatch.setattr(
        stop,
        "_load_transcript_with_retry",
        Mock(side_effect=AssertionError("transcript scanned")),
    )
    result = stop.handle(
        dict(
            session_id="s1",
            prompt="question",
            last_assistant_message="answer",
            transcript_path=__file__,
        )
    )
    assert result == ("queued", 0)
    assert len(_jobs(session_dir)) == 1
    adapter.publish.assert_not_called()


def test_codex_buffered_prompt_avoids_native_transcript_scan(
    adapter, session_dir, monkeypatch
):
    runtime.set_host(runtime.HOST_CODEX)
    state.append("s1", {"role": "User", "content": "real question"})
    monkeypatch.setattr(stop.ids, "resolve_user_id", lambda _: "project")
    monkeypatch.setattr(
        stop,
        "_load_transcript_with_retry",
        Mock(side_effect=AssertionError("transcript scanned")),
    )
    assert stop.handle(
        dict(session_id="s1", last_assistant_message="answer", transcript_path=__file__)
    ) == ("queued", 0)
    assert len(_jobs(session_dir)) == 1
    adapter.publish.assert_not_called()


def test_codex_native_message_still_filters_internal_prompt_recovered_from_transcript(
    adapter, session_dir, tmp_path
):
    runtime.set_host(runtime.HOST_CODEX)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            dict(
                type="user",
                message=dict(
                    content="You are a helpful assistant. You will be presented with a user prompt, and your job is to provide a short title for a task that will be created from that prompt."
                ),
            )
        )
        + "\n"
    )
    assert (
        stop.handle(
            dict(
                session_id="s1",
                last_assistant_message="Fix widget",
                transcript_path=str(transcript),
            )
        )
        is None
    )
    assert not _jobs(session_dir)
    assert state.read_all("s1") == []
    adapter.publish.assert_not_called()


def test_startup_kick_does_not_sweep_unbound_legacy_buffers(
    adapter, session_dir, monkeypatch
):
    _turn()
    spawned = Mock()
    monkeypatch.setattr(outbox, "_spawn_worker", spawned)
    outbox.kick()
    spawned.assert_not_called()
    adapter.publish.assert_not_called()


def test_concurrent_producers_admit_only_one_booting_child(
    adapter, session_dir, monkeypatch
):
    for session in ("s1", "s2"):
        _turn(session)
    spawn = Mock()
    monkeypatch.setattr(outbox, "_spawn_worker", spawn)
    ready = threading.Barrier(2)
    results = []

    def enqueue(session):
        ready.wait(2)
        results.append(outbox.enqueue(session, "project"))

    threads = [
        threading.Thread(target=enqueue, args=(session,)) for session in ("s1", "s2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert results == [True, True]
    assert len(_jobs(session_dir)) == 2
    spawn.assert_called_once()


def test_stale_launch_claim_recovers_on_next_wake(adapter, session_dir, monkeypatch):
    _turn()
    assert outbox.enqueue("s1", "project")
    claim = outbox._root() / "launch.claim"
    old = json.loads(claim.read_text())
    outbox._atomic_write(claim, dict(token=old["token"], created_at=0))
    spawn = Mock()
    monkeypatch.setattr(outbox, "_spawn_worker", spawn)
    outbox.kick()
    spawn.assert_called_once()
    assert json.loads(claim.read_text())["token"] != old["token"]


@pytest.mark.parametrize("matching", [False, True])
def test_worker_clears_only_its_own_launch_claim(adapter, session_dir, matching):
    _turn()
    adapter.publish.side_effect = lambda **_: PublishResult(False)
    assert outbox.enqueue("s1", "project")
    claim = outbox._root() / "launch.claim"
    token = json.loads(claim.read_text())["token"]
    outbox.run_worker(token if matching else "different-token")
    assert claim.exists() is not matching
    assert len(_jobs(session_dir)) == 1
    assert adapter.publish.call_count == int(matching)


def test_superseded_child_cannot_steal_owner_or_strand_healthy_handoff(
    adapter, session_dir, monkeypatch
):
    for session in ("s1", "s2"):
        _turn(session)
        assert outbox.enqueue(session, "project")
    claim = outbox._root() / "launch.claim"
    original = json.loads(claim.read_text())
    replacement = {**original, "token": "replacement-child"}
    outbox._atomic_write(claim, replacement)
    spawn = Mock()
    monkeypatch.setattr(outbox, "_spawn_worker", spawn)
    monkeypatch.setattr(outbox, "_MAX_BATCHES", 1)

    outbox.run_worker(original["token"])

    adapter.publish.assert_not_called()
    assert json.loads(claim.read_text()) == replacement
    with FileLock(outbox._root() / "worker.lock", timeout=0):
        pass
    outbox.run_worker(replacement["token"])
    assert adapter.publish.call_count == 1
    spawn.assert_called_once()
    successor = json.loads(claim.read_text())["token"]
    assert successor != replacement["token"]
    outbox.run_worker(successor)
    assert adapter.publish.call_count == 2
    assert not _jobs(session_dir)


def test_explicit_worker_without_token_does_not_bypass_pending_child(
    adapter, session_dir
):
    _turn()
    assert outbox.enqueue("s1", "project")
    outbox.run_worker()
    adapter.publish.assert_not_called()
    assert len(_jobs(session_dir)) == 1
    _worker()
    assert not _jobs(session_dir)


def test_spawn_uses_prepared_python_and_detaches_all_stdio(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(outbox.subprocess, "Popen", popen)
    _REAL_SPAWN("launch-token")
    args, kwargs = popen.call_args
    assert args[0] == [
        outbox.sys.executable,
        "-m",
        "claude_smart.publish_outbox",
        "launch-token",
    ]
    assert all(
        kwargs[key] == subprocess.DEVNULL for key in ("stdin", "stdout", "stderr")
    )
    assert kwargs["close_fds"]
    assert kwargs.get("start_new_session") or kwargs.get("creationflags")


def test_worker_observes_read_only_enabled_between_batches(adapter, session_dir):
    for session in ("s1", "s2"):
        _turn(session)
        assert outbox.enqueue(session, "project")

    def complete(**kw):
        env_config.CLAUDE_SMART_ENV_PATH.parent.mkdir(parents=True)
        env_config.CLAUDE_SMART_ENV_PATH.write_text('CLAUDE_SMART_READ_ONLY="1"\n')
        return PublishResult(True, kw["request_id"])

    adapter.publish.side_effect = complete
    _worker()
    assert adapter.publish.call_count == 1
    assert len(_jobs(session_dir)) == 1


def test_failed_generations_are_not_retried_by_continuations_before_healthy_job(
    adapter, session_dir, monkeypatch
):
    sessions = sorted((f"queued-{index}" for index in range(4)), key=outbox._digest)
    failures = set(sessions[:3])
    healthy = sessions[3]
    for session in sessions:
        _turn(session)
        assert outbox.enqueue(session, "project")
    adapter.publish.side_effect = lambda **kw: PublishResult(
        kw["session_id"] not in failures, kw["request_id"]
    )
    monkeypatch.setattr(outbox, "_MAX_BATCHES", 2)
    claim = outbox._root() / "launch.claim"
    for _ in range(5):
        if not claim.exists():
            break
        _worker()

    attempts = [call.kwargs["session_id"] for call in adapter.publish.call_args_list]
    assert attempts == sessions
    assert not claim.exists()
    assert {
        json.loads(path.read_text())["session_id"] for path in _jobs(session_dir)
    } == failures
    assert attempts.count(healthy) == 1

    # A new explicit wake is a fresh recovery round, rather than a permanent
    # suppression of failures recorded during the previous bounded handoff.
    outbox.kick()
    for _ in range(5):
        if not claim.exists():
            break
        _worker()
    attempts = [call.kwargs["session_id"] for call in adapter.publish.call_args_list]
    assert all(attempts.count(session) == 2 for session in failures)
    assert attempts.count(healthy) == 1
    assert not claim.exists()


def test_same_account_environment_change_cannot_rebind_held_generation(
    adapter, session_dir
):
    config = env_config.CLAUDE_SMART_ENV_PATH
    config.parent.mkdir(parents=True)
    original_config = 'CLAUDE_SMART_READ_ONLY="0"\n'
    config.write_text(original_config)
    _turn()
    assert outbox.enqueue("s1", "project")
    [job] = _jobs(session_dir)
    original_job = job.read_bytes()
    config.write_text(original_config + "# Different configuration authority\n")
    _turn(text="newer answer")

    assert not outbox.enqueue("s1", "project")
    assert job.read_bytes() == original_job
    _worker()
    adapter.publish.assert_not_called()
    assert job.read_bytes() == original_job

    config.write_text(original_config)
    outbox.kick()
    _worker()
    assert not _jobs(session_dir)
    assert adapter.publish.call_count == 1
    assert [
        turn["content"] for turn in adapter.publish.call_args.kwargs["interactions"]
    ] == ["question", "answer"]
    assert outbox.enqueue("s1", "project")
    _worker()
    assert adapter.publish.call_count == 2
