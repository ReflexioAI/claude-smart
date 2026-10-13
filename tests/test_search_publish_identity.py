# ruff: noqa: S101
"""Search identity must survive buffering, retries and later cancelled turns."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import time
from typing import Any, cast

import pytest

from claude_smart import publish, state
from claude_smart.reflexio_adapter import Adapter, PublishResult

pytestmark = pytest.mark.usefixtures("session_dir")


def _reserve_in_process(ready: Any, go: Any, results: Any) -> None:
    original = state.read_all

    def delayed_read(session_id: str) -> list[dict[str, Any]]:
        records = original(session_id)
        if not state.reserved_publish_requests(records):
            time.sleep(0.1)
        return records

    state.read_all = delayed_read
    ready.put(True)
    if not go.wait(10):
        return
    results.put(state.reserve_publish_request_id("s"))


def test_concurrent_processes_search_with_the_published_identity() -> None:
    turn("User")
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    go = context.Event()
    processes = [
        context.Process(target=_reserve_in_process, args=(ready, go, results))
        for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=10)
        go.set()
        search_ids = [results.get(timeout=10) for _ in processes]
        assert len(set(search_ids)) == 1
        turn("Assistant")
        recorder = Recorder()
        assert drain(recorder) == ("ok", 2)
        assert recorder.calls[0]["request_id"] == search_ids[0]
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        ready.close()
        results.close()


class Recorder:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_first = fail_first

    def publish(self, **kwargs: Any) -> PublishResult:
        self.calls.append(kwargs)
        if self.fail_first:
            self.fail_first = False
            return PublishResult(False)
        return PublishResult(True, kwargs["request_id"])


def turn(role: str) -> None:
    state.append("s", {"role": role, "content": "a real buffered turn", "ts": 1})


def drain(recorder: Recorder) -> tuple[str, int]:
    return publish.publish_unpublished(
        session_id="s",
        project_id="p",
        force_extraction=False,
        skip_aggregation=False,
        adapter=cast(Adapter, recorder),
    )


def test_search_identity_is_published_and_reused_after_failure() -> None:
    turn("User")
    search_id = state.reserve_publish_request_id("s")
    assert state.reserve_publish_request_id("s") == search_id
    turn("Assistant")
    recorder = Recorder(fail_first=True)
    assert drain(recorder)[0] == "failed"
    assert drain(recorder)[0] == "ok"
    assert [c["request_id"] for c in recorder.calls] == [search_id, search_id]


def test_new_search_never_binds_to_a_frozen_older_batch() -> None:
    turn("User")
    first = state.reserve_publish_request_id("s")
    turn("Assistant")
    recorder = Recorder(fail_first=True)
    drain(recorder)
    turn("User")
    second = state.reserve_publish_request_id("s")
    assert second != first
    turn("Assistant")
    assert drain(recorder) == ("ok", 4)
    assert [c["request_id"] for c in recorder.calls] == [first, first, second]
    assert all(len(c["interactions"]) == 2 for c in recorder.calls)


def test_cancelled_turn_does_not_attach_its_refs_to_later_response() -> None:
    turn("User")
    first = state.reserve_publish_request_id("s")
    state.append(
        "s",
        {"retrieved_learning_refs": [{"kind": "user_playbook", "learning_id": "1"}]},
    )
    turn("User")
    second = state.reserve_publish_request_id("s")
    state.append(
        "s",
        {"retrieved_learning_refs": [{"kind": "user_playbook", "learning_id": "2"}]},
    )
    turn("Assistant")
    recorder = Recorder()
    assert drain(recorder) == ("ok", 3)
    assert [c["request_id"] for c in recorder.calls] == [first, second]
    assert recorder.calls[1]["interactions"][-1]["retrieved_learnings"] == [
        {"kind": "user_playbook", "learning_id": "2"}
    ]


def test_parallel_searches_reserve_one_identity() -> None:
    turn("User")
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: state.reserve_publish_request_id("s"), range(12)))
    assert len(set(ids)) == 1


def test_search_hook_and_publish_share_identity_and_retrieval_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_smart import context_format, context_inject

    recorder = Recorder()
    search_calls = []

    class SearchAdapter:
        def search_all(self, **kwargs: Any) -> tuple[list[Any], list[Any], list[Any]]:
            search_calls.append(kwargs)
            return [], [], []

    monkeypatch.setattr(
        context_format,
        "render_inline_with_registry",
        lambda **_: (
            "supplied rule",
            [
                {
                    "kind": "playbook",
                    "source_kind": "user_playbook",
                    "real_id": "11",
                    "id": "rule-11",
                }
            ],
        ),
    )
    turn("User")
    assert context_inject.emit_context(
        session_id="s",
        project_id="p",
        query="question",
        hook_event_name="UserPromptSubmit",
        top_k=5,
        adapter=cast(Adapter, SearchAdapter()),
    )
    turn("Assistant")
    assert drain(recorder)[0] == "ok"
    assert recorder.calls[0]["request_id"] == search_calls[0]["request_id"]
    assert recorder.calls[0]["interactions"][-1]["retrieved_learnings"] == [
        {"kind": "user_playbook", "learning_id": "11"}
    ]


def test_search_only_cancelled_range_never_binds_to_a_later_prompt() -> None:
    first = state.reserve_publish_request_id("s")
    state.append(
        "s",
        {"retrieved_learning_refs": [{"kind": "user_playbook", "learning_id": "1"}]},
    )
    turn("User")
    second = state.reserve_publish_request_id("s")
    assert first != second
    turn("Assistant")
    recorder = Recorder()
    assert drain(recorder) == ("ok", 2)
    assert [c["request_id"] for c in recorder.calls] == [second]
    assert "retrieved_learnings" not in recorder.calls[0]["interactions"][-1]
