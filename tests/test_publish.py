"""Publish payload tests for locally injected learning links."""

from __future__ import annotations

from typing import Any, cast

from claude_smart import publish, state
from claude_smart.reflexio_adapter import Adapter


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


def _append_assistant(session_id: str, ts: int, content: str = "done") -> None:
    state.append(
        session_id,
        {"role": "Assistant", "ts": ts, "content": content, "user_id": "user"},
    )


def _publish(session_id: str, adapter: _RecordingAdapter) -> tuple[str, int]:
    return publish.publish_unpublished(
        session_id=session_id,
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=cast(Adapter, adapter),
    )


def test_publish_attaches_mapped_retrieved_learnings(session_dir) -> None:
    state.append_injected(
        "s1",
        [
            {
                "id": "p",
                "kind": "profile",
                "real_id": "profile-1",
                "title": "Prefers concise answers",
                "content": "Keep answers concise.",
                "ts": 10,
            },
            {
                "id": "u",
                "kind": "playbook",
                "source_kind": "user_playbook",
                "real_id": "11",
                "title": "Use pathlib",
                "source_title": "Filesystem discipline",
                "content": "Use pathlib for filesystem work.",
                "trigger": "writing Python filesystem code",
                "rationale": "Path objects are easier to compose.",
                "ts": 11,
            },
            {
                "id": "a",
                "kind": "playbook",
                "source_kind": "agent_playbook",
                "real_id": "22",
                "ts": 12,
            },
        ],
    )
    _append_assistant("s1", 20)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert adapter.calls[0]["interactions"][0]["retrieved_learnings"] == [
        {
            "kind": "profile",
            "learning_id": "profile-1",
            "snapshot": {
                "title": "Prefers concise answers",
                "content": "Keep answers concise.",
                "trigger": "",
                "rationale": "",
            },
        },
        {
            "kind": "user_playbook",
            "learning_id": "11",
            "snapshot": {
                "title": "Filesystem discipline",
                "content": "Use pathlib for filesystem work.",
                "trigger": "writing Python filesystem code",
                "rationale": "Path objects are easier to compose.",
            },
        },
        {"kind": "agent_playbook", "learning_id": "22"},
    ]


def test_publish_skips_old_entries_and_deduplicates(session_dir) -> None:
    state.append_injected(
        "s1",
        [
            {"id": "old", "kind": "profile", "ts": 1},
            {
                "id": "p1",
                "kind": "profile",
                "real_id": "profile-1",
                "title": "First",
                "content": "First injected wording.",
                "ts": 2,
            },
            {
                "id": "p2",
                "kind": "profile",
                "real_id": "profile-1",
                "title": "Second",
                "content": "Later duplicate wording.",
                "ts": 3,
            },
        ],
    )
    _append_assistant("s1", 10)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert adapter.calls[0]["interactions"][0]["retrieved_learnings"] == [
        {
            "kind": "profile",
            "learning_id": "profile-1",
            "snapshot": {
                "title": "First",
                "content": "First injected wording.",
                "trigger": "",
                "rationale": "",
            },
        }
    ]


def test_retrieved_learnings_attach_once_across_publishes(session_dir) -> None:
    state.append_injected(
        "s1", [{"id": "first", "kind": "profile", "real_id": "p1", "ts": 5}]
    )
    _append_assistant("s1", 10, "first")
    adapter = _RecordingAdapter()
    assert _publish("s1", adapter) == ("ok", 1)

    state.append_injected(
        "s1", [{"id": "second", "kind": "profile", "real_id": "p2", "ts": 15}]
    )
    _append_assistant("s1", 20, "second")
    assert _publish("s1", adapter) == ("ok", 1)

    assert adapter.calls[0]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p1"}
    ]
    assert adapter.calls[1]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p2"}
    ]


def test_registry_read_failure_does_not_break_publish(session_dir, monkeypatch) -> None:
    _append_assistant("s1", 10)
    monkeypatch.setattr(
        state, "read_injected_entries", lambda _session_id, _offset: 1 / 0
    )
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert "retrieved_learnings" not in adapter.calls[0]["interactions"][0]


def test_out_of_order_future_entry_does_not_block_eligible_entry(session_dir) -> None:
    state.append_injected(
        "s1",
        [
            {"id": "future", "kind": "profile", "real_id": "p2", "ts": 20},
            {"id": "eligible", "kind": "profile", "real_id": "p1", "ts": 5},
        ],
    )
    _append_assistant("s1", 10, "first")
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)
    assert adapter.calls[0]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p1"}
    ]

    _append_assistant("s1", 30, "second")
    assert _publish("s1", adapter) == ("ok", 1)
    assert adapter.calls[1]["interactions"][0]["retrieved_learnings"] == [
        {"kind": "profile", "learning_id": "p2"}
    ]


def test_publish_keeps_earliest_links_at_wire_cap(session_dir) -> None:
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
