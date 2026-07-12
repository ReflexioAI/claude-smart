"""Publish payload tests for locally injected learning links."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, cast

from claude_smart import publish, state
from claude_smart.reflexio_adapter import Adapter


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


class _SequencedAdapter(_RecordingAdapter):
    def __init__(self, results: list[bool]) -> None:
        super().__init__()
        self.results = iter(results)

    def publish(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return next(self.results)


class _CallbackAdapter(_RecordingAdapter):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self.callback = callback

    def publish(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        self.callback()
        return True


def _append_assistant(session_id: str, ts: int, content: str = "done") -> None:
    state.append(
        session_id,
        {"role": "Assistant", "ts": ts, "content": content, "user_id": "user"},
    )


def _append_user(session_id: str, ts: int, content: str = "question") -> None:
    state.append(
        session_id,
        {"role": "User", "ts": ts, "content": content, "user_id": "user"},
    )


def _inject_profile(session_id: str, learning_id: str, ts: int) -> None:
    state.append_injected(
        session_id,
        [
            {
                "id": f"rank-{learning_id}",
                "kind": "profile",
                "real_id": learning_id,
                "title": "Preference",
                "content": "Use the preference.",
                "ts": ts,
            }
        ],
    )


def _publish(session_id: str, adapter: _RecordingAdapter) -> tuple[str, int]:
    return publish.publish_unpublished(
        session_id=session_id,
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=cast(Adapter, adapter),
    )


def test_publish_attaches_identity_pairs_in_injection_order(session_dir) -> None:
    state.append_injected(
        "s1",
        [
            {"id": "p", "kind": "profile", "real_id": "profile-1", "ts": 10},
            {
                "id": "u",
                "kind": "playbook",
                "source_kind": "user_playbook",
                "real_id": "11",
                "ts": 11,
            },
            {
                "id": "a",
                "kind": "playbook",
                "source_kind": "agent_playbook",
                "real_id": "22",
                "ts": 12,
            },
            {"id": "invalid", "kind": "profile", "ts": 13},
            {"id": "duplicate", "kind": "profile", "real_id": "profile-1"},
        ],
    )
    _append_assistant("s1", 20)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert adapter.calls[0]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "profile-1"},
        {"kind": "user_playbook", "learning_id": "11"},
        {"kind": "agent_playbook", "learning_id": "22"},
    ]


def test_retrieved_learnings_attach_once_across_publishes(session_dir) -> None:
    _inject_profile("s1", "p1", 5)
    _append_assistant("s1", 10, "first")
    adapter = _RecordingAdapter()
    assert _publish("s1", adapter) == ("ok", 1)

    _inject_profile("s1", "p2", 15)
    _append_assistant("s1", 20, "second")
    assert _publish("s1", adapter) == ("ok", 1)

    assert adapter.calls[0]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p1"}
    ]
    assert adapter.calls[1]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p2"}
    ]
    assert adapter.calls[0]["request_id"] != adapter.calls[1]["request_id"]


def test_failed_publish_retries_frozen_batch_before_new_turns(session_dir) -> None:
    _inject_profile("s1", "p1", 5)
    _append_assistant("s1", 10, "first")
    adapter = _SequencedAdapter([False, True, True])

    assert _publish("s1", adapter) == ("failed", 1)

    _inject_profile("s1", "p2", 15)
    _append_assistant("s1", 20, "second")

    assert _publish("s1", adapter) == ("ok", 1)
    assert _publish("s1", adapter) == ("ok", 1)
    assert _publish("s1", adapter) == ("nothing", 0)

    assert [item["content"] for item in adapter.calls[0]["interactions"]] == [
        "first"
    ]
    assert adapter.calls[1]["interactions"] == adapter.calls[0]["interactions"]
    assert adapter.calls[1]["request_id"] == adapter.calls[0]["request_id"]
    assert [item["content"] for item in adapter.calls[2]["interactions"]] == [
        "second"
    ]
    assert adapter.calls[2]["request_id"] != adapter.calls[1]["request_id"]


def test_success_does_not_hide_turn_appended_during_publish(session_dir) -> None:
    _inject_profile("s1", "p1", 5)
    _append_assistant("s1", 10, "first")

    def append_next_turn() -> None:
        _inject_profile("s1", "p2", 15)
        _append_assistant("s1", 20, "second")

    first_adapter = _CallbackAdapter(append_next_turn)
    assert _publish("s1", first_adapter) == ("ok", 1)

    second_adapter = _RecordingAdapter()
    assert _publish("s1", second_adapter) == ("ok", 1)
    assert second_adapter.calls[0]["interactions"] == [
        {
            "role": "Assistant",
            "content": "second",
            "user_id": "user",
            "retrieved_learnings": [
                {"kind": "profile", "learning_id": "p2"}
            ],
        }
    ]


def test_prefix_uses_canonical_watermark_when_success_marker_is_later(
    session_dir,
) -> None:
    _append_assistant("s1", 1, "first")

    def append_unfinished_turn() -> None:
        _append_user("s1", 2, "second question")
        _inject_profile("s1", "p2", 3)

    first_adapter = _CallbackAdapter(append_unfinished_turn)
    assert _publish("s1", first_adapter) == ("ok", 1)

    second_adapter = _RecordingAdapter()
    assert _publish("s1", second_adapter) == ("ok", 1)
    assert [item["content"] for item in second_adapter.calls[0]["interactions"]] == [
        "second question"
    ]

    _append_assistant("s1", 4, "second")
    assert _publish("s1", second_adapter) == ("ok", 1)
    assert second_adapter.calls[1]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p2"}
    ]


def test_early_publish_keeps_refs_for_the_next_assistant(session_dir) -> None:
    _append_user("s1", 1)
    _inject_profile("s1", "p1", 2)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert "retrieved_learnings" not in adapter.calls[0]["interactions"][0]

    _append_assistant("s1", 3)
    assert _publish("s1", adapter) == ("ok", 1)
    assert adapter.calls[1]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p1"}
    ]


def test_overlapping_publishers_adopt_one_frozen_batch(
    session_dir, monkeypatch
) -> None:
    _inject_profile("s1", "p1", 1)
    _append_assistant("s1", 2, "first")

    first_marker_ready = threading.Event()
    allow_first_marker = threading.Event()
    second_request_ready = threading.Event()
    allow_second_request = threading.Event()
    original_append = state.append

    def append_with_pause(session_id: str, record: dict[str, Any]) -> None:
        if "publish_attempt" in record and threading.current_thread().name == "first":
            first_marker_ready.set()
            assert allow_first_marker.wait(timeout=5)
        original_append(session_id, record)

    monkeypatch.setattr(state, "append", append_with_pause)

    first_adapter = _RecordingAdapter()
    first_result: list[tuple[str, int]] = []
    first_thread = threading.Thread(
        name="first", target=lambda: first_result.append(_publish("s1", first_adapter))
    )
    first_thread.start()
    assert first_marker_ready.wait(timeout=5)

    _inject_profile("s1", "p2", 3)
    _append_assistant("s1", 4, "second")

    class BlockingAdapter(_RecordingAdapter):
        def publish(self, **kwargs: Any) -> bool:
            self.calls.append(kwargs)
            second_request_ready.set()
            assert allow_second_request.wait(timeout=5)
            return True

    second_adapter = BlockingAdapter()
    second_result: list[tuple[str, int]] = []
    second_thread = threading.Thread(
        name="second",
        target=lambda: second_result.append(_publish("s1", second_adapter)),
    )
    second_thread.start()
    assert second_request_ready.wait(timeout=5)

    allow_first_marker.set()
    first_thread.join(timeout=5)
    assert not first_thread.is_alive()
    allow_second_request.set()
    second_thread.join(timeout=5)
    assert not second_thread.is_alive()

    assert first_result == [("ok", 2)]
    assert second_result == [("ok", 2)]
    assert first_adapter.calls[0]["request_id"] == second_adapter.calls[0][
        "request_id"
    ]
    assert first_adapter.calls[0]["interactions"] == second_adapter.calls[0][
        "interactions"
    ]


def test_invalid_empty_attempt_marker_is_skipped(session_dir) -> None:
    state.append(
        "s1",
        {
            "retrieved_learning_refs": [
                {"kind": "profile", "learning_id": "p1"}
            ]
        },
    )
    state.append("s1", {"publish_attempt": {"start": 0, "end": 1}})
    _append_assistant("s1", 2)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert adapter.calls[0]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p1"}
    ]


def test_same_second_injections_follow_event_order(session_dir) -> None:
    _inject_profile("s1", "p1", 10)
    _append_assistant("s1", 10, "first")
    adapter = _RecordingAdapter()
    assert _publish("s1", adapter) == ("ok", 1)

    _inject_profile("s1", "p2", 10)
    _append_assistant("s1", 10, "second")
    assert _publish("s1", adapter) == ("ok", 1)

    assert adapter.calls[1]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p2"}
    ]


def test_publish_without_injected_learnings_keeps_typed_payload(session_dir) -> None:
    _append_assistant("s1", 10)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert "retrieved_learnings" not in adapter.calls[0]["interactions"][0]


def test_publish_keeps_first_links_at_request_cap(session_dir) -> None:
    state.append_injected(
        "s1",
        [
            {"id": str(index), "kind": "profile", "real_id": str(index), "ts": 1}
            for index in range(1002)
        ],
    )
    _append_assistant("s1", 10)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    retrieved = adapter.calls[0]["interactions"][0]["retrieved_learnings"]
    assert len(retrieved) == 1000
    assert retrieved[0]["learning_id"] == "0"
    assert retrieved[-1]["learning_id"] == "999"
