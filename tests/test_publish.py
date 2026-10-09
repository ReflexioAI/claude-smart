"""Publish payload tests for locally injected learning links."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, cast

import pytest
from claude_smart import publish, state
from claude_smart.reflexio_adapter import Adapter, PublishResult


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> PublishResult:
        self.calls.append(kwargs)
        return PublishResult(True, kwargs.get("request_id"))


class _SequencedAdapter(_RecordingAdapter):
    def __init__(self, results: list[bool]) -> None:
        super().__init__()
        self.results = iter(results)

    def publish(self, **kwargs: Any) -> PublishResult:
        self.calls.append(kwargs)
        result = next(self.results)
        return PublishResult(result, kwargs.get("request_id") if result else None)


class _CallbackAdapter(_RecordingAdapter):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self.callback = callback

    def publish(self, **kwargs: Any) -> PublishResult:
        self.calls.append(kwargs)
        self.callback()
        return PublishResult(True, kwargs.get("request_id"))


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


def test_success_records_request_id_for_local_lineage(session_dir) -> None:
    _append_assistant("s1", 10)
    adapter = _RecordingAdapter()

    assert _publish("s1", adapter) == ("ok", 1)

    marker = state.read_all("s1")[-1]
    assert marker["request_id"] == adapter.calls[0]["request_id"]
    assert marker["published_up_to"] == 1


def test_success_omits_unconfirmed_request_id_from_local_lineage(session_dir) -> None:
    class UnconfirmedAdapter:
        def publish(self, **_kwargs: Any) -> PublishResult:
            return PublishResult(True)

    _append_assistant("s1", 10)

    assert publish.publish_unpublished(
        session_id="s1",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=cast(Adapter, UnconfirmedAdapter()),
    ) == ("ok", 1)

    marker = state.read_all("s1")[-1]
    assert marker == {"published_up_to": 1}


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

    assert [item["content"] for item in adapter.calls[0]["interactions"]] == ["first"]
    assert adapter.calls[1]["interactions"] == adapter.calls[0]["interactions"]
    assert adapter.calls[1]["request_id"] == adapter.calls[0]["request_id"]
    assert [item["content"] for item in adapter.calls[2]["interactions"]] == ["second"]
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
            # `user_id` is buffer bookkeeping and is no longer put on the
            # wire (it is sent once at the request level). `created_at` is
            # deliberately not sent either — see state.unpublished_slice.
            "retrieved_learnings": [{"kind": "profile", "learning_id": "p2"}],
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
        def publish(self, **kwargs: Any) -> PublishResult:
            self.calls.append(kwargs)
            second_request_ready.set()
            assert allow_second_request.wait(timeout=5)
            return PublishResult(True, kwargs.get("request_id"))

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
    assert first_adapter.calls[0]["request_id"] == second_adapter.calls[0]["request_id"]
    assert (
        first_adapter.calls[0]["interactions"]
        == second_adapter.calls[0]["interactions"]
    )


def test_invalid_empty_attempt_marker_is_skipped(session_dir) -> None:
    state.append(
        "s1",
        {"retrieved_learning_refs": [{"kind": "profile", "learning_id": "p1"}]},
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


class _HostileWarningsClient:
    """A server response whose ``warnings`` blows up on access.

    Not contrived: a computed pydantic field or a lazy client wrapper that
    re-reads the socket on attribute access has exactly this shape, and
    ``getattr(obj, "warnings", None)`` absorbs only ``AttributeError``.
    """

    success = True

    @property
    def warnings(self) -> list[str]:
        raise RuntimeError("warnings unavailable")

    def publish_interaction(self, **_kwargs: Any) -> Any:
        return self

    def _make_request(self, _method: str, _path: str, **_kwargs: Any) -> Any:
        return self


def test_watermark_advances_even_if_reading_warnings_blows_up(session_dir) -> None:
    """A publish the server accepted must be marked published.

    This is the whole reason the adapter reads warnings outside the try that
    guards the publish call. If a diagnostic read could surface as a failed
    publish, the watermark would never advance and the next hook would re-send
    the same accepted batch — duplicates forever, caused by the observability
    code. Goes through the real Adapter rather than a fake so the guard itself
    is under test.
    """
    _append_assistant("s1", 10)
    adapter = Adapter()
    adapter._client = _HostileWarningsClient()  # bypass lazy construction

    assert publish.publish_unpublished(
        session_id="s1",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    ) == ("ok", 1)

    assert state.read_all("s1")[-1]["published_up_to"] == 1


def test_http_200_rejection_keeps_batch_retryable(session_dir) -> None:
    class Client:
        def _make_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"success": False, "message": "StorageError"}

    _append_user("rejected", 1)
    adapter = Adapter(url="https://example.com")
    adapter._client = Client()
    result = publish.publish_unpublished(
        session_id="rejected",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    assert result == ("failed", 1)
    records = state.read_all("rejected")
    assert state.published_record_offset(records) == 0
    assert state.pending_publish_end(records, 0) is not None


@pytest.mark.parametrize("failure", ["rejection", "timeout"])
def test_partial_chunk_failure_retries_same_ids_and_keeps_full_lineage(
    session_dir, failure
):
    class Client:
        def __init__(self):
            self.payloads = []
            self.queries = []
            self.saved = {}
            self.fail = True
            self.query_unavailable = False

        def _make_request(self, method, path, **kwargs):
            payload = kwargs["json"]
            if path == "/api/get_requests":
                self.queries.append(payload)
                assert set(payload) == {"request_id", "user_id", "session_id", "top_k"}
                assert payload["top_k"] == 1
                if self.query_unavailable:
                    self.query_unavailable = False
                    raise TimeoutError("confirmation query unavailable")
                saved = self.saved.get(payload["request_id"])
                return (
                    _stored_request_response(saved)
                    if saved
                    else {"success": True, "sessions": []}
                )
            self.payloads.append(payload)
            if payload["request_id"] in self.saved:
                return {"success": False, "message": "Request already exists"}
            if len(self.payloads) == 2 and self.fail:
                if failure == "timeout":
                    self.saved[payload["request_id"]] = payload
                    self.query_unavailable = True
                    raise TimeoutError("acknowledgement lost")
                return {"success": False}
            self.saved[payload["request_id"]] = payload
            return {"success": True}

    for i in range(998):
        _append_user("chunked", i, content=f"user-{i}")
    state.append(
        "chunked",
        {
            "role": "Assistant",
            "content": "done",
            "tools_used": [
                {"tool_name": "Bash", "tool_data": {"output": str(i)}}
                for i in range(2001)
            ],
        },
    )
    client = Client()
    adapter = Adapter(url="https://example.com")
    adapter._client = client
    kwargs: dict[str, Any] = dict(
        session_id="chunked",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    assert publish.publish_unpublished(**kwargs) == ("failed", 999)
    records = state.read_all("chunked")
    assert state.published_record_offset(records) == 0
    frozen_end = state.pending_publish_end(records, 0)
    assert frozen_end is not None
    _append_user("chunked", 1000, "later")
    client.fail = False
    assert publish.publish_unpublished(**kwargs) == ("recovered", 999)
    if failure == "rejection":
        assert client.payloads[1] == client.payloads[2]
    assert [len(p["interaction_data_list"]) for p in client.payloads] == (
        [1000, 1, 1] if failure == "rejection" else [1000, 1]
    )
    ids = [p["request_id"] for p in client.payloads[:2]]
    assert ids[0] != ids[1]
    marker = state.read_all("chunked")[-1]
    assert marker["published_up_to"] == frozen_end
    assert marker["request_id"] == ids[-1]
    assert marker["request_ids"] == ids
    saved = [
        x for batch in client.saved.values() for x in batch["interaction_data_list"]
    ]
    assert len(saved) == 1001
    assert [x["content"] for x in saved[:998]] == [f"user-{i}" for i in range(998)]
    assert [
        tool["tool_data"]["output"] for x in saved[998:] for tool in x["tools_used"]
    ] == [str(i) for i in range(2001)]
    assert publish.publish_unpublished(**kwargs) == ("ok", 1)
    assert client.payloads[-1]["request_id"] not in ids
    assert client.payloads[-1]["interaction_data_list"][0]["content"] == "later"


def test_null_key_collision_retains_frozen_batch(session_dir):
    state.append(
        "collision",
        {
            "role": "Assistant",
            "content": "done",
            "tools_used": [
                {
                    "tool_name": "Bash",
                    "tool_data": {"input": {"a\x00b": "first", "a\\u0000b": "second"}},
                }
            ],
        },
    )

    class Client:
        def _make_request(self, *args, **kwargs):
            raise AssertionError("colliding payload must not be sent")

    adapter = Adapter(url="https://example.com")
    adapter._client = Client()
    assert publish.publish_unpublished(
        session_id="collision",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    ) == ("failed", 1)
    records = state.read_all("collision")
    assert state.published_record_offset(records) == 0
    assert state.pending_publish_end(records, 0) is not None


def _stored_request_response(payload):
    from reflexio.models.api_schema.domain.entities import Interaction, Request
    from reflexio.models.api_schema.retriever_schema import GetRequestsViewResponse
    from reflexio.models.api_schema.ui.converters import to_interaction_view

    request = Request(
        **{
            key: payload[key]
            for key in (
                "request_id",
                "user_id",
                "session_id",
                "agent_version",
                "source",
                "evaluation_only",
            )
        }
    )
    interactions = [
        to_interaction_view(
            Interaction(
                user_id=payload["user_id"],
                request_id=payload["request_id"],
                interaction_id=i + 1,
                **({"created_at": 100} | item),
            )
        )
        for i, item in enumerate(payload["interaction_data_list"])
    ]
    return GetRequestsViewResponse.model_validate(
        {
            "success": True,
            "has_more": True,
            "sessions": [
                {
                    "session_id": payload["session_id"],
                    "requests": [{"request": request, "interactions": interactions}],
                }
            ],
        }
    ).model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize(
    "mismatch", ["content", "tools", "links", "scope", "unobservable"]
)
def test_existing_chunk_mismatch_keeps_watermark_unadvanced(session_dir, mismatch):
    class Client:
        def __init__(self):
            self.sent = []
            self.saved = None

        def _make_request(self, method, path, **kwargs):
            payload = kwargs["json"]
            if path == "/api/get_requests":
                if (
                    self.saved is None
                    or payload["request_id"] != self.saved["request_id"]
                ):
                    return {"success": True, "sessions": []}
                response = _stored_request_response(self.saved)
                stored = response["sessions"][0]["requests"][0]
                if mismatch == "content":
                    stored["interactions"][0]["content"] = "different"
                elif mismatch == "tools":
                    stored["interactions"][-1]["tools_used"] = []
                elif mismatch == "links":
                    stored["interactions"][-1]["retrieved_learnings"] = []
                elif mismatch == "scope":
                    stored["request"]["user_id"] = "another-project"
                return response
            self.sent.append(payload)
            if self.saved is None:
                self.saved = payload
                return {"success": True}
            return {"success": False}

    for i in range(999):
        _append_user("mismatch", i, str(i))
    assistant = {
        "role": "Assistant",
        "content": "done",
        "tools_used": [{"tool_name": "Bash", "tool_data": {"output": "activity"}}],
    }
    if mismatch == "links":
        assistant["retrieved_learnings"] = [
            {"kind": "profile", "learning_id": "profile-id"}
        ]
    if mismatch == "unobservable":
        assistant["image_encoding"] = "aGVsbG8="
    state.append("mismatch", assistant)
    _append_user("mismatch", 1001, "last")
    client = Client()
    adapter = Adapter(url="https://example.com")
    adapter._client = client
    kwargs: dict[str, Any] = dict(
        session_id="mismatch",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    assert publish.publish_unpublished(**kwargs) == ("failed", 1001)
    assert len(client.sent) == 2
    # Retry refuses to skip an existing request whose contents/scope cannot match.
    assert publish.publish_unpublished(**kwargs) == ("failed", 1001)
    assert len(client.sent) == 3
    assert client.sent[0] == client.sent[-1]
    assert state.published_record_offset(state.read_all("mismatch")) == 0


@pytest.mark.parametrize("mixed_empty", [False, True])
@pytest.mark.parametrize("learning_links", [False, True])
def test_single_committed_lost_ack_recovers_after_duplicate_rejection(
    session_dir, mixed_empty, learning_links
):
    class Client:
        def __init__(self):
            self.saved = None
            self.sent = []
            self.query_unavailable = True

        def _make_request(self, method, path, **kwargs):
            payload = kwargs["json"]
            if path == "/api/get_requests":
                if self.query_unavailable:
                    self.query_unavailable = False
                    raise TimeoutError("temporary read failure")
                assert self.saved is not None
                assert payload["request_id"] == self.saved["request_id"]
                return _stored_request_response(self.saved)
            self.sent.append(payload)
            if self.saved is not None:
                return {"success": False, "message": "Request already exists"}
            self.saved = payload
            raise TimeoutError("committed acknowledgement lost")

    _append_user("single", 1, "question")
    if mixed_empty:
        _append_assistant("single", 2, "  ")
    if learning_links:
        _inject_profile("single", "profile-id", 2)
    _append_assistant("single", 3, "answer")
    client = Client()
    adapter = Adapter(url="https://example.com")
    adapter._client = client
    kwargs: dict[str, Any] = dict(
        session_id="single",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    count = 3 if mixed_empty else 2
    assert publish.publish_unpublished(**kwargs) == ("failed", count)
    assert state.published_record_offset(state.read_all("single")) == 0
    from reflexio.models.api_schema.domain.entities import InteractionData

    expected = (
        "failed"
        if learning_links and "retrieved_learnings" not in InteractionData.model_fields
        else "recovered"
    )
    assert publish.publish_unpublished(**kwargs) == (expected, count)
    assert client.sent[0] == client.sent[1]
    if learning_links:
        assert client.sent[0]["interaction_data_list"][-1]["retrieved_learnings"] == [
            {"kind": "profile", "learning_id": "profile-id"}
        ]
    if expected == "failed":
        # An older read/schema contract cannot prove that links were retained.
        assert state.published_record_offset(state.read_all("single")) == 0
        return
    assert client.saved is not None
    assert [item["content"] for item in client.saved["interaction_data_list"]] == [
        "question",
        "answer",
    ]
    assert state.read_all("single")[-1]["request_id"] == client.saved["request_id"]
    assert publish.publish_unpublished(**kwargs) == ("nothing", 0)


def test_empty_only_batch_is_retired_without_network(session_dir):
    class Client:
        def _make_request(self, *args, **kwargs):
            raise AssertionError("empty placeholders must not be sent")

    _append_assistant("empty", 1, "  ")
    adapter = Adapter(url="https://example.com")
    adapter._client = Client()
    assert publish.publish_unpublished(
        session_id="empty",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    ) == ("nothing", 0)
    assert state.read_all("empty")[-1] == {"published_up_to": 1}


def test_large_fresh_publish_succeeds_despite_read_outage_and_replay_fails_closed(
    session_dir,
):
    class Client:
        def __init__(self):
            self.sent = []
            self.saved = set()

        def _make_request(self, method, path, **kwargs):
            if path == "/api/get_requests":
                raise TimeoutError("read API unavailable")
            payload = kwargs["json"]
            self.sent.append(payload)
            if payload["request_id"] in self.saved:
                return {"success": False, "message": "Request already exists"}
            self.saved.add(payload["request_id"])
            return {"success": True}

    for i in range(1001):
        _append_user("unreadable", i, str(i))
    client = Client()
    adapter = Adapter(url="https://example.com")
    adapter._client = client
    kwargs: dict[str, Any] = dict(
        session_id="unreadable",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    assert publish.publish_unpublished(**kwargs) == ("ok", 1001)
    records = state.read_all("unreadable")
    marker = records[-1]
    assert marker["published_up_to"] == 1001
    assert len(marker["request_ids"]) == 2
    # Simulate loss of the local watermark after remote durable acceptance.
    import json

    state.session_path("unreadable").write_text(
        "\n".join(json.dumps(row) for row in records[:-1]) + "\n"
    )
    assert publish.publish_unpublished(**kwargs) == ("failed", 1001)
    assert state.published_record_offset(state.read_all("unreadable")) == 0
    assert client.sent[0] == client.sent[-1]


@pytest.mark.parametrize(
    "timestamps,mismatch",
    [
        (True, None),
        (False, None),
        (True, "timestamp"),
        (False, (True, 1)),
        (False, (False, 0)),
        (False, (1, True)),
        (False, (0, False)),
    ],
)
def test_lost_ack_confirmation_preserves_ingestion_order_timestamps_and_json_types(
    timestamps, mismatch
):
    class Client:
        def __init__(self):
            self.saved = None
            self.sent = []
            self.read_unavailable = True

        def _make_request(self, method, path, **kwargs):
            payload = kwargs["json"]
            if path == "/api/get_requests":
                if self.read_unavailable:
                    self.read_unavailable = False
                    raise TimeoutError("confirmation unavailable")
                assert self.saved is not None
                response = _stored_request_response(self.saved)
                items = response["sessions"][0]["requests"][0]["interactions"]
                if mismatch == "timestamp":
                    items[0]["created_at"] = 201
                elif isinstance(mismatch, tuple):
                    items[0]["tools_used"][0]["tool_data"]["input"]["nested"][0][
                        "value"
                    ] = mismatch[1]
                # Read ordering is not relied upon: ingestion IDs establish wire order.
                items.reverse()
                return response
            self.sent.append(payload)
            if self.saved is not None:
                return {"success": False, "message": "Request already exists"}
            self.saved = payload
            raise TimeoutError("committed acknowledgement lost")

    value = mismatch[0] if isinstance(mismatch, tuple) else True
    interactions: list[dict[str, Any]] = [
        {
            "role": "Assistant",
            "content": "first",
            "tools_used": [
                {
                    "tool_name": "Bash",
                    "tool_data": {"input": {"nested": [{"value": value}]}},
                }
            ],
        },
        {"role": "Assistant", "content": "second"},
    ]
    if timestamps:
        interactions[0]["created_at"] = 200
        interactions[1]["created_at"] = 100
    client = Client()
    adapter = Adapter(url="https://example.com")
    adapter._client = client
    kwargs: dict[str, Any] = dict(
        session_id="ordered",
        project_id="project",
        request_id="stable",
        interactions=interactions,
    )
    assert not adapter.publish(**kwargs)
    result = adapter.publish(**kwargs)
    assert bool(result) is (mismatch is None)
    assert client.sent[0] == client.sent[1]
    if mismatch is None:
        assert result.request_id == "stable"
    else:
        assert result.error_type == "ServerRejected"


@pytest.mark.parametrize(
    "view,include_refs",
    [
        ("legacy", False),
        ("legacy", True),
        ("modern", False),
        ("modern", True),
        ("modern-mismatch", False),
        ("modern-mismatch", True),
        ("modern-ref-mismatch", True),
    ],
)
@pytest.mark.parametrize(
    "statuses",
    [
        ("success", "error"),
        (None, 23),
        (True, {"reason": "failure"}),
        ("x" * 120, ""),
    ],
)
def test_captured_tool_status_recovers_with_older_sdk_and_legacy_or_modern_view(
    session_dir, view, include_refs, statuses
):
    class Client:
        def __init__(self):
            self.saved = None
            self.sent = []
            self.read_unavailable = True

        def _make_request(self, method, path, **kwargs):
            payload = kwargs["json"]
            if path == "/api/get_requests":
                if self.read_unavailable:
                    self.read_unavailable = False
                    raise TimeoutError("confirmation unavailable")
                assert self.saved is not None
                response = _stored_request_response(self.saved)
                stored_items = response["sessions"][0]["requests"][0]["interactions"]
                for stored, original in zip(
                    stored_items, self.saved["interaction_data_list"], strict=True
                ):
                    if view == "legacy":
                        stored.pop("retrieved_learnings", None)
                    else:
                        stored["retrieved_learnings"] = [
                            dict(ref) for ref in original.get("retrieved_learnings", [])
                        ]
                    for tool, original_tool in zip(
                        stored.get("tools_used", []),
                        original.get("tools_used", []),
                        strict=True,
                    ):
                        if view == "legacy":
                            tool.pop("status", None)
                        else:
                            value = original_tool.get("status")
                            # Modern backend View coercion, independent of local SDK.
                            tool["status"] = "" if value is None else str(value)[:100]
                if view == "modern-mismatch":
                    stored_items[-1]["tools_used"][0]["status"] = "different"
                elif view == "modern-ref-mismatch":
                    stored_items[-1]["retrieved_learnings"][0]["learning_id"] = (
                        "different-profile"
                    )
                return response
            self.sent.append(payload)
            if self.saved is not None:
                return {"success": False, "message": "Request already exists"}
            self.saved = payload
            raise TimeoutError("committed acknowledgement lost")

    state.append("captured-tools", {"role": "User", "content": "question"})
    if include_refs:
        _inject_profile("captured-tools", "profile-id", 1)
    for index, status in enumerate(statuses):
        state.append(
            "captured-tools",
            {
                "role": "Assistant_tool",
                "tool_name": "Bash",
                "tool_input": {"command": f"echo {index}", "nested": [{"ok": True}]},
                "tool_output": f"result-{index}",
                "status": status,
            },
        )
    state.append("captured-tools", {"role": "Assistant", "content": "done"})
    watermark_end = len(state.read_all("captured-tools"))
    client = Client()
    adapter = Adapter(url="https://example.com")
    adapter._client = client
    kwargs: dict[str, Any] = dict(
        session_id="captured-tools",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    assert publish.publish_unpublished(**kwargs) == ("failed", 2)
    fails = view in {"modern-mismatch", "modern-ref-mismatch"} or (
        view == "legacy" and include_refs
    )
    expected_status = "failed" if fails else "recovered"
    assert publish.publish_unpublished(**kwargs) == (expected_status, 2)
    assert client.sent[0] == client.sent[1]
    tools = client.sent[0]["interaction_data_list"][-1]["tools_used"]
    assert [tool["status"] for tool in tools] == list(statuses)
    assert state.published_record_offset(state.read_all("captured-tools")) == (
        0 if fails else watermark_end
    )


@pytest.mark.parametrize(
    "ack", [None, {}, {"success": None}, {"success": 1}, {"success": "true"}]
)
@pytest.mark.parametrize("committed", [False, True])
def test_malformed_ack_advances_watermark_only_after_exact_storage_confirmation(
    session_dir, ack, committed
):
    class Client:
        def __init__(self):
            self.saved = None

        def _make_request(self, method, path, **kwargs):
            if path == "/api/get_requests":
                return (
                    _stored_request_response(self.saved)
                    if self.saved is not None
                    else {"success": True, "sessions": []}
                )
            if committed:
                self.saved = kwargs["json"]
            return ack

    _append_user("malformed-ack", 1, "question")
    adapter = Adapter(url="https://example.com")
    adapter._client = Client()
    assert publish.publish_unpublished(
        session_id="malformed-ack",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    ) == ("recovered" if committed else "failed", 1)
    records = state.read_all("malformed-ack")
    assert state.published_record_offset(records) == (1 if committed else 0)
    if not committed:
        assert state.pending_publish_end(records, 0) is not None


@pytest.mark.parametrize("storage_succeeds", [False, True])
@pytest.mark.parametrize("message_count", [1, 1001])
def test_real_publish_route_requires_storage_before_watermark(
    session_dir, monkeypatch, storage_succeeds, message_count
):
    import asyncio
    import importlib
    import inspect

    from fastapi import BackgroundTasks
    from reflexio.models.api_schema.domain.entities import (
        PublishUserInteractionRequest,
        PublishUserInteractionResponse,
    )
    from reflexio.server.routes import interactions as routes
    from starlette.requests import Request as HttpRequest

    route = inspect.unwrap(routes.publish_user_interaction)
    legacy = "background_tasks" in inspect.signature(route).parameters
    saved = {}

    def persist(*, org_id, request, **kwargs):
        if not storage_succeeds:
            return PublishUserInteractionResponse(
                success=False, message="Storage failed"
            )
        if request.request_id in saved:
            return PublishUserInteractionResponse(
                success=False, message="Request already exists"
            )
        saved[request.request_id] = request.model_dump(mode="json")
        return PublishUserInteractionResponse(success=True)

    monkeypatch.setattr(routes.publisher_api, "add_user_interaction", persist)
    if not legacy:
        waiting = importlib.import_module(
            "reflexio.server.services.durable_learning.waiting"
        )

        async def acquire(*args):
            return True

        monkeypatch.setattr(waiting, "acquire_ingestion", acquire)
        monkeypatch.setattr(waiting, "release_ingestion", lambda *args: None)

        # wait_for_response=False must never wait for extraction.
        def unexpected_waiter(*args):
            raise AssertionError("publish must not wait for extraction")

        monkeypatch.setattr(waiting, "acquire_waiter", unexpected_waiter)

    class Client:
        def __init__(self):
            self.backgrounds = []
            self.ids = []

        def _make_request(self, method, path, **kwargs):
            payload = kwargs["json"]
            if path == "/api/get_requests":
                stored = saved.get(payload["request_id"])
                return (
                    _stored_request_response(stored)
                    if stored
                    else {"success": True, "sessions": []}
                )
            self.ids.append(payload["request_id"])
            route_kwargs: dict[str, Any] = dict(
                request=HttpRequest(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/api/publish_interaction",
                        "headers": [],
                        "client": ("127.0.0.1", 1),
                    }
                ),
                payload=PublishUserInteractionRequest(**payload),
                org_id="org",
                wait_for_response=False,
                _gate=None,
            )
            if legacy:
                background = BackgroundTasks()
                route_kwargs["background_tasks"] = background
                response = route(**route_kwargs)
                self.backgrounds.append(background)
            else:
                response = asyncio.run(route(**route_kwargs))
                # Real modern route awaits the mocked ingestion before its ACK.
                assert (payload["request_id"] in saved) == storage_succeeds
            return response.model_dump(mode="json")

    for index in range(message_count):
        _append_user("route-storage", index + 1, f"question {index}")
    client = Client()
    adapter = Adapter(url="https://example.com")
    adapter._client = client
    kwargs: dict[str, Any] = dict(
        session_id="route-storage",
        project_id="project",
        force_extraction=False,
        skip_aggregation=False,
        adapter=adapter,
    )
    chunks = (message_count + 999) // 1000
    result = publish.publish_unpublished(**kwargs)
    assert result == (
        "ok" if not legacy and storage_succeeds else "failed",
        message_count,
    )
    if legacy:
        assert saved == {}
        assert state.published_record_offset(state.read_all("route-storage")) == 0
        asyncio.run(client.backgrounds[-1]())
        assert len(saved) == (1 if storage_succeeds else 0)
        for chunk_index in range(chunks):
            result = publish.publish_unpublished(**kwargs)
            complete = storage_succeeds and chunk_index == chunks - 1
            assert result == ("recovered" if complete else "failed", message_count)
            assert state.published_record_offset(state.read_all("route-storage")) == (
                message_count if complete else 0
            )
            if not complete or chunks == 1:
                asyncio.run(client.backgrounds[-1]())
        assert len(saved) == (chunks if storage_succeeds else 0)
        # Every retry targets the same deterministic chunk IDs; no new records.
        assert len(set(client.ids)) == (chunks if storage_succeeds else 1)
        if chunks == 1:
            assert client.ids[0] == client.ids[1]
    else:
        assert len(saved) == (chunks if storage_succeeds else 0)
        assert state.published_record_offset(state.read_all("route-storage")) == (
            message_count if storage_succeeds else 0
        )
