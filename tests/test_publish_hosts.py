"""Replay recovery and the poison-batch breaker on every host's publish path.

Claude Code and OpenCode drain synchronously from the Stop handler; Codex's
Stop only enqueues, and the detached outbox worker drains. Each test drives the
real handler (and, for Codex, the real worker) against a fake transport.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from claude_smart import publish, publish_outbox, runtime, state
from claude_smart.events import session_end, stop
from claude_smart.reflexio_adapter import Adapter

from tests.test_publish import _stored_request_response

HOSTS = [runtime.HOST_CLAUDE_CODE, runtime.HOST_OPENCODE, runtime.HOST_CODEX]


class _HookKilled(BaseException):
    """The host killed the publishing process mid-request."""


class _ConflictResponse:
    status_code = 409

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id

    def json(self) -> dict[str, Any]:
        # reflexio #807: routes/interactions.py raises HTTPException(409, detail=...)
        return {
            "detail": {
                "reason": "request_id_conflict",
                "request_id": self.request_id,
                "message": f"request_id {self.request_id!r} already exists for a "
                "different publish; use a new request_id",
            }
        }


class _HTTPError(Exception):
    def __init__(self, response: Any) -> None:
        super().__init__(f"{response.status_code} Client Error")
        self.response = response


class _Server:
    """Stores what it accepts; optionally dies once or rejects a poison batch.

    ``shape`` selects the reused-request_id contract: ``legacy`` servers answer
    200 ``success: false`` "already exists"; ``modern`` ones (reflexio #807)
    replay an identical retry and answer 409 for different content.
    """

    def __init__(
        self,
        *,
        kill_first: bool = False,
        poison: str | None = None,
        shape: str = "legacy",
        store_altered: bool = False,
    ):
        self.kill_first = kill_first
        self.poison = poison
        self.shape = shape
        self.store_altered = store_altered
        self.saved: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, Any]] = []

    def _make_request(self, method: str, path: str, **kwargs: Any) -> Any:
        payload = kwargs["json"]
        if path == "/api/finalize_session":
            return {"success": True}
        if path == "/api/get_requests":
            stored = self.saved.get(payload["request_id"])
            if stored is None:
                return {"success": True, "sessions": []}
            return _stored_request_response(stored)
        self.posts.append(payload)
        if self.poison and any(
            item.get("content") == self.poison
            for item in payload["interaction_data_list"]
        ):
            return {"success": False, "message": "interaction rejected"}
        request_id = payload["request_id"]
        if request_id in self.saved:
            if self.shape == "legacy":
                return {
                    "success": False,
                    "message": f"request_id '{request_id}' already exists",
                }
            if self.saved[request_id] != payload:
                raise _HTTPError(_ConflictResponse(request_id))
            return {
                "success": True,
                "replayed": True,
                "request_id": request_id,
                "message": "Interaction already published with this request_id; "
                "nothing new was stored",
            }
        if self.store_altered:
            # An earlier delivery under this ID carried different content.
            payload = json.loads(json.dumps(payload))
            payload["interaction_data_list"][0]["content"] = "something else"
        self.saved[request_id] = payload
        if self.kill_first:
            self.kill_first = False
            raise _HookKilled
        return {"success": True}


@pytest.fixture
def host_env(session_dir, monkeypatch, request):
    host = request.param
    runtime.set_host(host)
    server = _Server()

    def adapter() -> Adapter:
        instance = Adapter(url="https://example.com")
        instance._client = server
        return instance

    monkeypatch.setattr(publish, "Adapter", adapter)
    monkeypatch.setattr(publish_outbox, "Adapter", adapter)
    monkeypatch.setattr(stop.ids, "resolve_user_id", lambda *_a, **_kw: "project")
    return host, server


def _stop(host: str, tmp_path, session_id: str, prompt: str, answer: str) -> None:
    """Run one turn through the host's real Stop path, then its drainer."""
    state.append(session_id, {"role": "User", "content": prompt, "ts": 1})
    payload: dict[str, Any] = {"session_id": session_id, "cwd": str(tmp_path)}
    if host == runtime.HOST_CLAUDE_CODE:
        transcript = tmp_path / f"{session_id}-transcript.jsonl"
        entries = [
            {"type": "user", "message": {"content": prompt}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": answer}]},
            },
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        payload["transcript_path"] = str(transcript)
    else:
        payload["last_assistant_message"] = answer
    result = stop.handle(payload)
    if host == runtime.HOST_CODEX:
        assert result == ("queued", 0)
        # Run the worker the enqueue admitted, as its detached process would.
        claim = publish_outbox._root() / "launch.claim"
        token = json.loads(claim.read_text())["token"] if claim.exists() else None
        publish_outbox.run_worker(token)


def _prime_citation(session_id: str) -> str:
    state.append_injected(
        session_id,
        [
            {
                "id": "p1-uuid",
                "kind": "profile",
                "title": "prefers anyio",
                "content": "prefers anyio",
                "real_id": "uuid-anyio",
                "ts": 0,
            }
        ],
    )
    return "Done.\n\n✨ 1 claude-smart learning applied [cs:p1-uuid]"


@pytest.mark.parametrize("shape", ["legacy", "modern"])
@pytest.mark.parametrize("host_env", HOSTS, indirect=True)
def test_killed_delivery_with_citations_recovers_on_next_stop(
    host_env, tmp_path, shape
):
    """Legacy: verified recovery after 200 "already exists". Modern: replayed."""
    host, server = host_env
    server.kill_first = True
    server.shape = shape
    answer = _prime_citation("s")

    with pytest.raises(_HookKilled):
        _stop(host, tmp_path, "s", "first question", answer)
    first = server.posts[0]
    assert first["interaction_data_list"][-1]["citations"]
    assert state.published_record_offset(state.read_all("s")) == 0

    _stop(host, tmp_path, "s", "second question", "second answer")
    records = state.read_all("s")
    watermarks = [r for r in records if "published_up_to" in r]
    assert watermarks[0]["request_id"] == first["request_id"]
    # The frozen batch was replayed once, recognised as stored, and released.
    assert [p["request_id"] for p in server.posts].count(first["request_id"]) == 2


@pytest.mark.parametrize("host_env", HOSTS, indirect=True)
def test_poison_batch_is_parked_and_the_session_drains(host_env, tmp_path, monkeypatch):
    host, server = host_env
    server.poison = "poison question"
    now = [1_000_000.0]
    monkeypatch.setattr(publish.time, "time", lambda: now[0])

    for turn in range(publish.POISON_REJECTION_LIMIT):
        prompt = "poison question" if turn == 0 else f"later question {turn}"
        _stop(host, tmp_path, "p", prompt, f"answer {turn}")
        now[0] += publish.POISON_MIN_SPAN_SECONDS / (publish.POISON_REJECTION_LIMIT - 1)
    records = state.read_all("p")
    parked = [r for r in records if "publish_dead_letter" in r]
    assert len(parked) == 1
    dead_letter = parked[0]["publish_dead_letter"]
    assert dead_letter["reason"] == "interaction rejected"
    assert dead_letter["rejections"] == publish.POISON_REJECTION_LIMIT

    # Turns queued behind the parked batch now reach the server.
    _stop(host, tmp_path, "p", "after parking", "fine")
    accepted = [
        item["content"]
        for payload in server.saved.values()
        for item in payload["interaction_data_list"]
    ]
    assert "after parking" in accepted
    assert "poison question" not in accepted


def test_session_end_drain_is_bounded_by_the_host_budget(session_dir, monkeypatch):
    seen: dict[str, Any] = {}

    def capture(**kwargs: Any) -> tuple[str, int]:
        seen.update(kwargs)
        return ("deferred", 0)

    finalized: list[str] = []
    monkeypatch.setattr(session_end.publish, "publish_unpublished", capture)
    monkeypatch.setattr(
        session_end.Adapter,
        "finalize_session",
        lambda self, **kw: finalized.append(kw["session_id"]) or True,
    )
    monkeypatch.setattr(session_end.ids, "resolve_user_id", lambda *_a, **_kw: "p")
    import time

    before = time.monotonic()
    session_end.handle({"session_id": "s"})
    assert (
        before
        < seen["deadline"]
        <= (time.monotonic() + session_end._SESSION_END_PUBLISH_BUDGET_SECONDS)
    )
    # Claude Code caps the shared SessionEnd budget at 60 seconds.
    assert session_end._SESSION_END_PUBLISH_BUDGET_SECONDS + 10 <= 60
    # A deferred drain is not a completed session.
    assert finalized == []


def _hook_log(tmp_path) -> list[dict[str, Any]]:
    path = tmp_path / "hook.log"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("host_env", HOSTS, indirect=True)
def test_replayed_success_is_recorded_as_recovered(host_env, tmp_path):
    host, server = host_env
    server.kill_first = True
    server.shape = "modern"
    with pytest.raises(_HookKilled):
        _stop(host, tmp_path, "r", "first question", "first answer")
    _stop(host, tmp_path, "r", "second question", "second answer")
    results = [r for r in _hook_log(tmp_path) if r["event"] == "publish-result"]
    assert results[0]["publish_status"] == "recovered"
    assert results[0]["host"] == host
    assert state.published_record_offset(state.read_all("r")) > 0


@pytest.mark.parametrize("host_env", HOSTS, indirect=True)
def test_request_id_conflict_parks_the_batch_on_first_occurrence(host_env, tmp_path):
    host, server = host_env
    server.kill_first = True
    server.shape = "modern"
    server.store_altered = True
    with pytest.raises(_HookKilled):
        _stop(host, tmp_path, "c", "first question", "first answer")
    server.store_altered = False

    _stop(host, tmp_path, "c", "second question", "second answer")
    records = state.read_all("c")
    parked = [r["publish_dead_letter"] for r in records if "publish_dead_letter" in r]
    assert len(parked) == 1
    assert parked[0]["reason"].startswith("request_id_conflict: ")
    assert parked[0]["rejections"] == 1
    failed = [
        r
        for r in _hook_log(tmp_path)
        if r["event"] == "publish-result" and r["publish_status"] == "failed"
    ]
    assert failed[-1]["error_type"] == "RequestIdConflict"
    assert failed[-1]["http_status"] == 409
    assert failed[-1]["error_reason"] == "request_id_conflict"
    assert "different publish" in failed[-1]["server_message"]

    # Later turns are no longer blocked behind the conflicting batch.
    _stop(host, tmp_path, "c", "after conflict", "fine")
    accepted = [
        item["content"]
        for payload in server.saved.values()
        for item in payload["interaction_data_list"]
    ]
    assert "after conflict" in accepted
