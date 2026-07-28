"""Tests for the thin reflexio adapter."""

from __future__ import annotations

import logging
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from claude_smart import reflexio_adapter


class _FakeClient:
    """Minimal stand-in for ReflexioClient used in these tests."""

    def __init__(
        self,
        *,
        publish_ok: bool = True,
        user_playbook_resp=None,
        agent_playbook_resp=None,
        profile_resp=None,
    ):
        self._publish_ok = publish_ok
        self._user_playbook_resp = user_playbook_resp
        self._agent_playbook_resp = agent_playbook_resp
        self._profile_resp = profile_resp
        self.published_kwargs: dict[str, Any] = {}
        self.raw_request: dict[str, Any] = {}

    def publish_interaction(self, **kwargs):
        self.published_kwargs = kwargs
        if not self._publish_ok:
            raise RuntimeError("reflexio unreachable")

    def _make_request(self, method, path, **kwargs):
        self.raw_request = {"method": method, "path": path, **kwargs}
        if not self._publish_ok:
            raise RuntimeError("reflexio unreachable")

    def get_user_playbooks(self, **_kw):
        return self._user_playbook_resp

    def get_agent_playbooks(self, **_kw):
        return self._agent_playbook_resp

    def get_profiles(self, **_kw):
        return self._profile_resp


def _adapter_with(client) -> reflexio_adapter.Adapter:
    a = reflexio_adapter.Adapter()
    a._client = client  # bypass lazy constructor
    return a


def test_adapter_constructs_client_with_env_url_and_api_key(
    monkeypatch, tmp_path
) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="rflx-test-key"\n'
    )
    monkeypatch.setattr(reflexio_adapter.env_config, "CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.delenv("REFLEXIO_URL", raising=False)
    monkeypatch.delenv("REFLEXIO_API_KEY", raising=False)
    seen: dict[str, str] = {}

    class FakeReflexioClient:
        def __init__(self, *, url_endpoint: str, api_key: str, timeout: int):
            seen["url_endpoint"] = url_endpoint
            seen["api_key"] = api_key

    monkeypatch.setitem(
        sys.modules,
        "reflexio",
        SimpleNamespace(ReflexioClient=FakeReflexioClient),
    )

    adapter = reflexio_adapter.Adapter()

    assert adapter._get_client() is not None
    assert seen == {
        "url_endpoint": "https://www.reflexio.ai/",
        "api_key": "rflx-test-key",
    }


def test_adapter_passes_short_http_timeout_to_client(monkeypatch, tmp_path) -> None:
    """Hooks must cap reflexio HTTP calls so an unhealthy backend can't stall a session."""
    monkeypatch.setattr(
        reflexio_adapter.env_config, "CLAUDE_SMART_ENV_PATH", tmp_path / "missing.env"
    )
    monkeypatch.setenv("REFLEXIO_URL", "https://x.example/")
    monkeypatch.setenv("REFLEXIO_API_KEY", "k")
    seen: dict[str, Any] = {}

    class FakeReflexioClient:
        def __init__(self, *, url_endpoint: str, api_key: str, timeout: int):
            seen["timeout"] = timeout

    monkeypatch.setitem(
        sys.modules,
        "reflexio",
        SimpleNamespace(ReflexioClient=FakeReflexioClient),
    )

    reflexio_adapter.Adapter()._get_client()

    assert seen["timeout"] == reflexio_adapter._HTTP_TIMEOUT_SECONDS
    assert 0 < reflexio_adapter._HTTP_TIMEOUT_SECONDS <= 30


def test_adapter_process_env_overrides_reflexio_env(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://from-file.example/"\nREFLEXIO_API_KEY="file-key"\n'
    )
    monkeypatch.setattr(reflexio_adapter.env_config, "CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.setenv("REFLEXIO_URL", "https://from-env.example/")
    monkeypatch.setenv("REFLEXIO_API_KEY", "env-key")
    seen: dict[str, str] = {}

    class FakeReflexioClient:
        def __init__(self, *, url_endpoint: str, api_key: str, timeout: int):
            seen["url_endpoint"] = url_endpoint
            seen["api_key"] = api_key

    monkeypatch.setitem(
        sys.modules,
        "reflexio",
        SimpleNamespace(ReflexioClient=FakeReflexioClient),
    )

    adapter = reflexio_adapter.Adapter()

    assert adapter._get_client() is not None
    assert seen == {
        "url_endpoint": "https://from-env.example/",
        "api_key": "env-key",
    }


def test_adapter_default_url_follows_backend_port(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        reflexio_adapter.env_config, "CLAUDE_SMART_ENV_PATH", tmp_path / "missing.env"
    )
    monkeypatch.delenv("REFLEXIO_URL", raising=False)
    monkeypatch.delenv("REFLEXIO_API_KEY", raising=False)
    monkeypatch.setenv("BACKEND_PORT", "8171")
    seen: dict[str, str] = {}

    class FakeReflexioClient:
        def __init__(self, *, url_endpoint: str, api_key: str, timeout: int):
            seen["url_endpoint"] = url_endpoint
            seen["api_key"] = api_key

    monkeypatch.setitem(
        sys.modules,
        "reflexio",
        SimpleNamespace(ReflexioClient=FakeReflexioClient),
    )

    assert reflexio_adapter.Adapter()._get_client() is not None
    assert seen == {"url_endpoint": "http://localhost:8171/", "api_key": ""}


def test_adapter_local_default_file_url_follows_backend_port(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text('REFLEXIO_URL="http://localhost:8071/"\n')
    monkeypatch.setattr(reflexio_adapter.env_config, "CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.delenv("REFLEXIO_URL", raising=False)
    monkeypatch.delenv("REFLEXIO_API_KEY", raising=False)
    monkeypatch.setenv("BACKEND_PORT", "8171")
    seen: dict[str, str] = {}

    class FakeReflexioClient:
        def __init__(self, *, url_endpoint: str, api_key: str, timeout: int):
            seen["url_endpoint"] = url_endpoint
            seen["api_key"] = api_key

    monkeypatch.setitem(
        sys.modules,
        "reflexio",
        SimpleNamespace(ReflexioClient=FakeReflexioClient),
    )

    assert reflexio_adapter.Adapter()._get_client() is not None
    assert seen == {"url_endpoint": "http://localhost:8171/", "api_key": ""}


def test_publish_returns_true_on_success() -> None:
    client = _FakeClient()
    a = _adapter_with(client)
    ok = a.publish(
        session_id="s1",
        project_id="p1",
        interactions=[{"role": "User", "content": "hi"}],
        force_extraction=True,
        override_learning_stall=True,
    )
    assert ok.ok is True
    # After the project-scoped preferences refactor (commit 88cb150), reflexio's
    # ``user_id`` is the project slug and ``session_id`` is sent separately.
    assert client.published_kwargs["user_id"] == "p1"
    assert client.published_kwargs["session_id"] == "s1"
    assert client.published_kwargs["agent_version"] == "claude-code"
    assert client.published_kwargs["source"] == "claude-smart"
    assert client.published_kwargs["wait_for_response"] is False
    assert client.published_kwargs["force_extraction"] is True
    assert client.published_kwargs["override_learning_stall"] is True


def test_link_publish_uses_stable_raw_request() -> None:
    client = _FakeClient()
    adapter = _adapter_with(client)

    ok = adapter.publish(
        session_id="s1",
        project_id="p1",
        request_id="request-1",
        interactions=[
            {
                "role": "Assistant",
                "content": "done",
                "retrieved_learnings": [
                    {"kind": "profile", "learning_id": "profile-1"}
                ],
            }
        ],
    )

    assert ok.ok is True
    assert client.published_kwargs == {}
    assert client.raw_request["method"] == "POST"
    assert client.raw_request["path"] == "/api/publish_interaction"
    assert client.raw_request["json"]["request_id"] == "request-1"
    assert client.raw_request["json"]["source"] == "claude-smart"
    assert client.raw_request["json"]["interaction_data_list"][0][
        "retrieved_learnings"
    ] == [{"kind": "profile", "learning_id": "profile-1"}]


def test_publish_with_request_id_uses_stable_raw_request() -> None:
    client = _FakeClient()
    adapter = _adapter_with(client)

    ok = adapter.publish(
        session_id="s1",
        project_id="p1",
        request_id="request-1",
        interactions=[{"role": "User", "content": "hi"}],
    )

    assert ok.ok is True
    assert client.published_kwargs == {}
    assert client.raw_request["json"]["request_id"] == "request-1"
    assert client.raw_request["json"]["interaction_data_list"] == [
        {"role": "User", "content": "hi"}
    ]


def test_publish_confirmation_is_scoped_to_each_shared_adapter_call() -> None:
    class ConcurrentRawClient:
        def __init__(self) -> None:
            self.barrier = threading.Barrier(2)

        def publish_interaction(self, *, source="", **_kwargs):
            raise AssertionError("stable request IDs use the raw path")

        def _make_request(self, _method, _path, **_kwargs):
            self.barrier.wait(timeout=5)

    adapter = _adapter_with(ConcurrentRawClient())
    results = {}

    def publish(request_id: str) -> None:
        results[request_id] = adapter.publish(
            session_id=request_id,
            project_id="p1",
            request_id=request_id,
            interactions=[{"role": "User", "content": request_id}],
        )

    threads = [
        threading.Thread(target=publish, args=(request_id,))
        for request_id in ("request-1", "request-2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert {request_id: result.request_id for request_id, result in results.items()} == {
        "request-1": "request-1",
        "request-2": "request-2",
    }


def test_public_publish_records_server_generated_request_id() -> None:
    class PublicOnlyClient:
        def publish_interaction(self, **_kwargs):
            return SimpleNamespace(request_id="server-request")

    adapter = _adapter_with(PublicOnlyClient())

    ok = adapter.publish(
        session_id="s1",
        project_id="p1",
        request_id="local-request",
        interactions=[{"role": "User", "content": "hi"}],
    )

    assert ok.ok is True
    assert ok.request_id == "server-request"


def test_missing_raw_request_helper_falls_back_without_optional_links(
    monkeypatch,
) -> None:
    client = _FakeClient()
    monkeypatch.setattr(client, "_make_request", None)
    adapter = _adapter_with(client)

    ok = adapter.publish(
        session_id="s1",
        project_id="p1",
        interactions=[
            {
                "role": "Assistant",
                "content": "done",
                "retrieved_learnings": [
                    {"kind": "profile", "learning_id": "profile-1"}
                ],
            }
        ],
    )

    assert ok.ok is True
    assert client.published_kwargs["interactions"] == [
        {"role": "Assistant", "content": "done"}
    ]


def test_ambiguous_raw_failure_retries_base_with_same_request_id() -> None:
    class ResponseLossClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.raw_requests = []

        def _make_request(self, method, path, **kwargs):
            self.raw_requests.append(kwargs["json"])
            if len(self.raw_requests) == 1:
                raise TimeoutError("response was lost after send")

        def publish_interaction(self, **kwargs):
            raise AssertionError("fallback must preserve the stable request ID")

    client = ResponseLossClient()
    adapter = _adapter_with(client)

    ok = adapter.publish(
        session_id="s1",
        project_id="p1",
        request_id="stable-request",
        interactions=[
            {
                "role": "Assistant",
                "content": "done",
                "retrieved_learnings": [
                    {"kind": "profile", "learning_id": "profile-1"}
                ],
            }
        ],
    )

    assert ok.ok is True
    assert len(client.raw_requests) == 2
    assert {request["request_id"] for request in client.raw_requests} == {
        "stable-request"
    }
    assert client.raw_requests[0]["interaction_data_list"][0][
        "retrieved_learnings"
    ] == [{"kind": "profile", "learning_id": "profile-1"}]
    assert client.raw_requests[1]["interaction_data_list"] == [
        {"role": "Assistant", "content": "done"}
    ]


def test_ambiguous_raw_failure_does_not_retry_through_public_sdk() -> None:
    class ResponseLossClient(_FakeClient):
        def _make_request(self, _method, _path, **_kwargs):
            raise TimeoutError("response was lost after send")

        def publish_interaction(self, **_kwargs):
            raise AssertionError("SDK retry could duplicate the stable request")

    result = _adapter_with(ResponseLossClient()).publish(
        session_id="s1",
        project_id="p1",
        request_id="stable-request",
        interactions=[{"role": "User", "content": "hi"}],
    )

    assert not result.ok
    assert result.request_id is None


def test_publish_returns_false_when_client_raises() -> None:
    a = _adapter_with(_FakeClient(publish_ok=False))
    ok = a.publish(
        session_id="s1",
        project_id="p1",
        interactions=[{"role": "User", "content": "hi"}],
        force_extraction=False,
    )
    assert ok.ok is False


def test_publish_trivially_true_when_no_interactions() -> None:
    a = _adapter_with(_FakeClient())
    assert a.publish(session_id="s", project_id="p", interactions=[]).ok is True


def test_fetch_user_playbooks_reads_user_playbooks_field() -> None:
    resp = SimpleNamespace(user_playbooks=[{"content": "rule"}])
    a = _adapter_with(_FakeClient(user_playbook_resp=resp))
    assert a.fetch_user_playbooks(project_id="p1") == [{"content": "rule"}]


def test_fetch_agent_playbooks_reads_agent_playbooks_field() -> None:
    resp = SimpleNamespace(
        agent_playbooks=[{"content": "global rule", "playbook_status": "approved"}]
    )
    a = _adapter_with(_FakeClient(agent_playbook_resp=resp))
    assert a.fetch_agent_playbooks() == [
        {"content": "global rule", "playbook_status": "approved"}
    ]


def test_fetch_agent_playbooks_filters_rejected_status() -> None:
    resp = SimpleNamespace(
        agent_playbooks=[
            {"content": "pending", "playbook_status": "pending"},
            {"content": "approved", "playbook_status": "approved"},
            {"content": "rejected", "playbook_status": "rejected"},
        ]
    )
    a = _adapter_with(_FakeClient(agent_playbook_resp=resp))
    assert a.fetch_agent_playbooks() == [
        {"content": "pending", "playbook_status": "pending"},
        {"content": "approved", "playbook_status": "approved"},
    ]


def test_fetch_profiles_reads_user_profiles_field() -> None:
    resp = SimpleNamespace(user_profiles=[{"content": "pref"}])
    a = _adapter_with(_FakeClient(profile_resp=resp))
    assert a.fetch_project_profiles("p1") == [{"content": "pref"}]


def test_fetch_profiles_calls_scoped_get_profiles() -> None:
    class ScopedClient:
        def __init__(self):
            self.profile_kwargs: dict[str, Any] = {}

        def get_profiles(self, **kwargs):
            self.profile_kwargs = kwargs
            return SimpleNamespace(user_profiles=[{"user_id": "proj", "content": "p"}])

    client = ScopedClient()
    a = _adapter_with(client)

    assert a.fetch_project_profiles("proj", top_k=3) == [
        {"user_id": "proj", "content": "p"}
    ]
    assert client.profile_kwargs == {
        "user_id": "proj",
        "top_k": 3,
        "status_filter": [None],
    }


def test_fetch_helpers_return_empty_on_unknown_shape() -> None:
    a = _adapter_with(
        _FakeClient(
            user_playbook_resp=object(),
            agent_playbook_resp=object(),
            profile_resp=object(),
        )
    )
    assert a.fetch_user_playbooks(project_id="p1") == []
    assert a.fetch_agent_playbooks() == []
    assert a.fetch_project_profiles("p1") == []


def test_publish_returns_false_when_client_unavailable(monkeypatch) -> None:
    a = reflexio_adapter.Adapter()
    monkeypatch.setattr(a, "_get_client", lambda: None)
    assert not a.publish(
        session_id="s",
        project_id="p",
        interactions=[{"role": "User", "content": "x"}],
    ).ok


# -----------------------------------------------------------------------------
# Query-aware unified search and broad fetch
# -----------------------------------------------------------------------------


class _RecordingClient:
    """Captures kwargs of every search call; returns the canned response."""

    def __init__(
        self,
        *,
        unified_resp=None,
        user_playbook_resp=None,
        agent_playbook_resp=None,
        profile_resp=None,
    ):
        self._unified_resp = unified_resp
        self._user_playbook_resp = user_playbook_resp
        self._agent_playbook_resp = agent_playbook_resp
        self._profile_resp = profile_resp
        self.search_kwargs: dict[str, Any] = {}
        self.search_call_count = 0
        self.user_playbook_kwargs: dict[str, Any] = {}
        self.agent_playbook_kwargs: dict[str, Any] = {}
        self.profile_kwargs: dict[str, Any] = {}

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        self.search_call_count += 1
        return self._unified_resp

    def get_user_playbooks(self, **kwargs):
        self.user_playbook_kwargs = kwargs
        return self._user_playbook_resp

    def get_agent_playbooks(self, **kwargs):
        self.agent_playbook_kwargs = kwargs
        return self._agent_playbook_resp

    def get_profiles(self, **kwargs):
        self.profile_kwargs = kwargs
        return self._profile_resp


def test_search_all_calls_unified_endpoint_with_project_scope() -> None:
    client = _RecordingClient(
        unified_resp=SimpleNamespace(
            user_playbooks=[{"content": "u"}],
            agent_playbooks=[
                {"content": "a-pending", "playbook_status": "pending"},
                {"content": "a-approved", "playbook_status": "approved"},
                {"content": "a-rejected", "playbook_status": "rejected"},
            ],
            profiles=[{"content": "p"}],
        )
    )
    a = _adapter_with(client)
    user_pb, agent_pb, profiles = a.search_all(
        project_id="proj", query="config.toml", top_k=3
    )
    assert user_pb == [{"content": "u"}]
    assert agent_pb == [
        {"content": "a-pending", "playbook_status": "pending"},
        {"content": "a-approved", "playbook_status": "approved"},
    ]
    assert profiles == [{"content": "p"}]
    # user_id scopes user playbooks + preferences; agent playbooks have no
    # user_id field so the same filter no-ops on that leg server-side.
    assert client.search_kwargs["user_id"] == "proj"
    assert client.search_kwargs["agent_version"] == "claude-code"
    assert client.search_kwargs["query"] == "config.toml"
    assert client.search_kwargs["top_k"] == 3
    assert client.search_kwargs["search_mode"] == "hybrid"
    assert client.search_kwargs["entity_types"] == [
        "profiles",
        "user_playbooks",
        "agent_playbooks",
    ]
    assert client.search_kwargs["agent_playbook_status_filter"] == [
        "pending",
        "approved",
    ]
    assert client.search_kwargs["enable_agent_answer"] is False


def test_search_all_makes_one_client_call() -> None:
    """Server-side fan-out: a single /api/search hits all three legs."""
    client = _RecordingClient(
        unified_resp=SimpleNamespace(user_playbooks=[], agent_playbooks=[], profiles=[])
    )
    a = _adapter_with(client)
    a.search_all(project_id="p", query="q")
    assert client.search_call_count == 1


def test_search_all_passes_session_id_for_server_side_dedup() -> None:
    """session_id scopes the server's per-session result dedup."""
    client = _RecordingClient(
        unified_resp=SimpleNamespace(user_playbooks=[], agent_playbooks=[], profiles=[])
    )
    a = _adapter_with(client)
    a.search_all(project_id="p", query="q", session_id="sess-1")
    assert client.search_kwargs["session_id"] == "sess-1"


def test_search_all_session_id_defaults_to_none() -> None:
    client = _RecordingClient(
        unified_resp=SimpleNamespace(user_playbooks=[], agent_playbooks=[], profiles=[])
    )
    a = _adapter_with(client)
    a.search_all(project_id="p", query="q")
    assert client.search_kwargs["session_id"] is None


def test_search_all_returns_three_empty_lists_on_error() -> None:
    class BrokenSearch:
        def search(self, **_kw):
            raise RuntimeError("unified search down")

    a = _adapter_with(BrokenSearch())
    user_pb, agent_pb, profiles = a.search_all(project_id="p", query="q")
    assert (user_pb, agent_pb, profiles) == ([], [], [])


def test_search_all_returns_three_empty_lists_when_client_unavailable(
    monkeypatch,
) -> None:
    a = reflexio_adapter.Adapter()
    monkeypatch.setattr(a, "_get_client", lambda: None)
    assert a.search_all(project_id="p", query="q") == ([], [], [])


def test_fetch_all_returns_three_lists() -> None:
    client = _RecordingClient(
        user_playbook_resp=SimpleNamespace(user_playbooks=[{"content": "u"}]),
        agent_playbook_resp=SimpleNamespace(agent_playbooks=[{"content": "a"}]),
        profile_resp=SimpleNamespace(user_profiles=[{"content": "p"}]),
    )
    a = _adapter_with(client)
    user_pb, agent_pb, profiles = a.fetch_all(project_id="proj")
    assert user_pb == [{"content": "u"}]
    assert agent_pb == [{"content": "a"}]
    assert profiles == [{"content": "p"}]
    # User playbooks and preferences scoped to project_id; agent playbooks
    # have no user_id filter (global).
    assert client.user_playbook_kwargs["user_id"] == "proj"
    assert "user_id" not in client.agent_playbook_kwargs
    assert client.agent_playbook_kwargs["agent_version"] == "claude-code"
    assert client.profile_kwargs["user_id"] == "proj"


def test_fetch_all_runs_legs_in_parallel() -> None:
    """Serial would be ~0.6s across three legs; parallel should stay near 0.2s."""

    class SlowClient:
        def get_user_playbooks(self, **_kw):
            time.sleep(0.2)
            return SimpleNamespace(user_playbooks=[])

        def get_agent_playbooks(self, **_kw):
            time.sleep(0.2)
            return SimpleNamespace(agent_playbooks=[])

        def get_profiles(self, **_kw):
            time.sleep(0.2)
            return SimpleNamespace(user_profiles=[])

    a = _adapter_with(SlowClient())
    t0 = time.perf_counter()
    a.fetch_all(project_id="p")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.35, f"legs did not run in parallel (elapsed={elapsed:.3f}s)"


def test_fetch_all_absorbs_per_leg_failure() -> None:
    class HalfBroken:
        def get_user_playbooks(self, **_kw):
            raise RuntimeError("user playbook search down")

        def get_agent_playbooks(self, **_kw):
            return SimpleNamespace(agent_playbooks=[{"content": "a"}])

        def get_profiles(self, **_kw):
            return SimpleNamespace(user_profiles=[{"content": "p"}])

    a = _adapter_with(HalfBroken())
    user_pb, agent_pb, profiles = a.fetch_all(project_id="p")
    assert user_pb == []
    assert agent_pb == [{"content": "a"}]
    assert profiles == [{"content": "p"}]


def test_fetch_all_prefers_no_query_list_endpoints_for_show() -> None:
    class ListClient:
        def __init__(self):
            self.user_kwargs: dict[str, Any] = {}
            self.agent_kwargs: dict[str, Any] = {}
            self.profile_kwargs: dict[str, Any] = {}

        def get_user_playbooks(self, **kwargs):
            self.user_kwargs = kwargs
            return SimpleNamespace(user_playbooks=[{"content": "u"}])

        def get_agent_playbooks(self, **kwargs):
            self.agent_kwargs = kwargs
            return SimpleNamespace(
                agent_playbooks=[{"content": "a", "playbook_status": "approved"}]
            )

        def get_profiles(self, **kwargs):
            self.profile_kwargs = kwargs
            return SimpleNamespace(
                user_profiles=[{"user_id": "proj", "content": "p"}]
            )

    client = ListClient()
    a = _adapter_with(client)

    user_pb, agent_pb, profiles = a.fetch_all(project_id="proj")

    assert user_pb == [{"content": "u"}]
    assert agent_pb == [{"content": "a", "playbook_status": "approved"}]
    assert profiles == [{"user_id": "proj", "content": "p"}]
    assert client.user_kwargs == {
        "user_id": "proj",
        "status_filter": [None],
        "limit": 10,
    }
    assert client.agent_kwargs == {
        "agent_version": "claude-code",
        "status_filter": [None],
        "limit": 10,
    }
    assert client.profile_kwargs == {
        "user_id": "proj",
        "top_k": 20,
        "status_filter": [None],
    }


def test_fetch_user_playbooks_passes_project_id_and_default_top_k() -> None:
    """/show uses a narrow broad-fetch default; prompt-time search carries specificity."""
    client = _RecordingClient(user_playbook_resp=SimpleNamespace(user_playbooks=[]))
    a = _adapter_with(client)
    a.fetch_user_playbooks(project_id="proj")
    assert client.user_playbook_kwargs["user_id"] == "proj"
    assert client.user_playbook_kwargs["limit"] == 10
    assert client.user_playbook_kwargs["status_filter"] == [None]


def test_fetch_agent_playbooks_is_global() -> None:
    client = _RecordingClient(agent_playbook_resp=SimpleNamespace(agent_playbooks=[]))
    a = _adapter_with(client)
    a.fetch_agent_playbooks()
    assert "user_id" not in client.agent_playbook_kwargs
    assert client.agent_playbook_kwargs["agent_version"] == "claude-code"
    assert client.agent_playbook_kwargs["status_filter"] == [None]
    assert client.agent_playbook_kwargs["limit"] == 10


def test_search_all_filters_rejected_locally() -> None:
    """Belt-and-suspenders: if the unified search backend ignores
    ``agent_playbook_status_filter`` (older reflexio, future regression), the
    plugin still drops REJECTED agent playbooks before they reach injection."""
    client = _RecordingClient(
        unified_resp=SimpleNamespace(
            user_playbooks=[{"content": "u"}],
            agent_playbooks=[
                {"content": "approved", "playbook_status": "approved"},
                {"content": "rejected", "playbook_status": "rejected"},
                {"content": "pending", "playbook_status": "pending"},
            ],
            profiles=[],
        )
    )
    a = _adapter_with(client)
    _, agent_pb, _ = a.search_all(project_id="p", query="q")
    contents = [pb["content"] for pb in agent_pb]
    assert contents == ["approved", "pending"]
    assert "rejected" not in contents


def test_fetch_agent_playbooks_filters_rejected_locally() -> None:
    """Same defensive filter on the no-query /show path."""

    class StubClient:
        def get_agent_playbooks(self, **_kw):
            return SimpleNamespace(
                agent_playbooks=[
                    {"content": "approved", "playbook_status": "approved"},
                    {
                        "content": "rejected",
                        "playbook_status": "REJECTED",
                    },  # case-insensitive
                ]
            )

    a = _adapter_with(StubClient())
    agent_pb = a.fetch_agent_playbooks()
    contents = [pb["content"] for pb in agent_pb]
    assert contents == ["approved"]


# -----------------------------------------------------------------------------
# apply_extraction_defaults — push claude-smart's preferred cadence to reflexio
# -----------------------------------------------------------------------------


class _ConfigClient:
    """Captures get_config/set_config calls against a mutable config object."""

    def __init__(self, *, window_size: int, stride_size: int, get_raises=None):
        self._config = SimpleNamespace(window_size=window_size, stride_size=stride_size)
        self._get_raises = get_raises
        self.set_calls: list[SimpleNamespace] = []

    def get_config(self):
        if self._get_raises is not None:
            raise self._get_raises
        return self._config

    def set_config(self, config):
        self.set_calls.append(
            SimpleNamespace(
                window_size=config.window_size, stride_size=config.stride_size
            )
        )
        self._config = config
        return {"ok": True}


def test_apply_extraction_defaults_writes_when_values_differ() -> None:
    client = _ConfigClient(window_size=10, stride_size=5)
    a = _adapter_with(client)
    assert a.apply_extraction_defaults(window_size=5, stride_size=3) is True
    assert len(client.set_calls) == 1
    assert client.set_calls[0].window_size == 5
    assert client.set_calls[0].stride_size == 3


def test_apply_extraction_defaults_skips_set_when_values_match() -> None:
    client = _ConfigClient(window_size=5, stride_size=3)
    a = _adapter_with(client)
    assert a.apply_extraction_defaults(window_size=5, stride_size=3) is True
    assert client.set_calls == []


def test_apply_extraction_defaults_returns_false_when_client_unavailable(
    monkeypatch,
) -> None:
    a = reflexio_adapter.Adapter()
    monkeypatch.setattr(a, "_get_client", lambda: None)
    assert a.apply_extraction_defaults(window_size=5, stride_size=3) is False


def test_apply_extraction_defaults_absorbs_get_config_errors() -> None:
    a = _adapter_with(
        _ConfigClient(window_size=10, stride_size=5, get_raises=RuntimeError("down"))
    )
    assert a.apply_extraction_defaults(window_size=5, stride_size=3) is False


# -----------------------------------------------------------------------------
# apply_optimizer_defaults — opt-in Reflexio optimizer wiring
# -----------------------------------------------------------------------------


class _OptimizerConfigClient:
    def __init__(self, *, opt=None, get_raises=None):
        self._config = SimpleNamespace(
            playbook_optimizer_config=opt
            or SimpleNamespace(
                enabled=False,
                optimize_user_playbooks=False,
                optimize_agent_playbooks=True,
                auto_update_user_playbooks=False,
                min_commit_windows=2,
                assistant_script_path=None,
                assistant_script_args=["old"],
                webhook_url="https://example.test/assistant",
                webhook_timeout_seconds=60,
            )
        )
        self._get_raises = get_raises
        self.set_calls: list[Any] = []

    def get_config(self):
        if self._get_raises is not None:
            raise self._get_raises
        return self._config

    def set_config(self, config):
        self.set_calls.append(config)
        self._config = config
        return {"ok": True}


def test_apply_optimizer_defaults_writes_required_fields() -> None:
    client = _OptimizerConfigClient()
    a = _adapter_with(client)

    assert a.apply_optimizer_defaults(script_path="/venv/bin/assistant") is True

    assert len(client.set_calls) == 1
    opt = client.set_calls[0].playbook_optimizer_config
    assert opt.enabled is True
    assert opt.optimize_user_playbooks is False
    assert opt.optimize_agent_playbooks is True
    assert opt.auto_update_user_playbooks is True
    assert opt.min_commit_windows == 1
    assert opt.max_metric_calls == 15
    assert opt.assistant_script_path == "/venv/bin/assistant"
    assert opt.assistant_script_args == []
    assert opt.webhook_url is None
    assert opt.webhook_timeout_seconds == 300


def test_apply_optimizer_defaults_skips_set_when_values_match() -> None:
    opt = SimpleNamespace(
        enabled=True,
        optimize_user_playbooks=False,
        optimize_agent_playbooks=True,
        auto_update_user_playbooks=True,
        min_commit_windows=1,
        max_metric_calls=15,
        assistant_script_path="/venv/bin/assistant",
        assistant_script_args=[],
        webhook_url=None,
        webhook_timeout_seconds=300,
    )
    client = _OptimizerConfigClient(opt=opt)
    a = _adapter_with(client)

    assert a.apply_optimizer_defaults(script_path="/venv/bin/assistant") is True

    assert client.set_calls == []


def test_apply_optimizer_defaults_returns_false_when_client_unavailable(
    monkeypatch,
) -> None:
    a = reflexio_adapter.Adapter()
    monkeypatch.setattr(a, "_get_client", lambda: None)

    assert a.apply_optimizer_defaults(script_path="/venv/bin/assistant") is False


def test_apply_optimizer_defaults_absorbs_get_config_errors() -> None:
    a = _adapter_with(_OptimizerConfigClient(get_raises=RuntimeError("down")))

    assert a.apply_optimizer_defaults(script_path="/venv/bin/assistant") is False


# -----------------------------------------------------------------------------
# fetch_stall_state / mark_stall_notified
# -----------------------------------------------------------------------------


class _StubClientWithStallState:
    def __init__(self, response):
        self._response = response
        self.notified_called = False

    def get_stall_state(self):
        return self._response

    def mark_stall_notified(self):
        self.notified_called = True


def test_fetch_stall_state_returns_none_when_client_unavailable():
    adapter = reflexio_adapter.Adapter(url="http://nope/")
    adapter._client = None  # type: ignore[attr-defined]
    # _get_client returns None when reflexio unimportable / unreachable
    assert adapter.fetch_stall_state() is None


def test_fetch_stall_state_passes_through_when_clean(monkeypatch):
    stub = _StubClientWithStallState(
        SimpleNamespace(
            stalled=False,
            reason=None,
            stalled_at=None,
            reset_estimate=None,
            notified_in_cc=False,
            error_message=None,
        )
    )
    adapter = reflexio_adapter.Adapter(url="http://x/")
    monkeypatch.setattr(adapter, "_get_client", lambda: stub)
    state = adapter.fetch_stall_state()
    assert state is not None
    assert state.stalled is False


def test_mark_stall_notified_calls_through(monkeypatch):
    stub = _StubClientWithStallState(None)
    adapter = reflexio_adapter.Adapter(url="http://x/")
    monkeypatch.setattr(adapter, "_get_client", lambda: stub)
    adapter.mark_stall_notified()
    assert stub.notified_called is True


_ADAPTER_LOGGER = "claude_smart.reflexio_adapter"


class _WarningClient(_FakeClient):
    """Fake whose publish paths echo a server ``warnings`` payload."""

    def __init__(self, *, response: Any = None, raw_response: Any = None) -> None:
        super().__init__()
        self._response = response
        self._raw_response = raw_response

    def publish_interaction(self, **kwargs):
        self.published_kwargs = kwargs
        return self._response

    def _make_request(self, method, path, **kwargs):
        self.raw_request = {"method": method, "path": path, **kwargs}
        return self._raw_response


class _WarningsPropertyRaises:
    """``getattr(obj, "warnings", None)`` absorbs only ``AttributeError``."""

    @property
    def warnings(self) -> list[str]:
        raise RuntimeError("warnings unavailable")


class _MappingGetRaises(dict):
    """``isinstance(response, dict)`` holds, but the override is what runs."""

    def get(self, *_args, **_kwargs):
        raise KeyError("boom")


class _Unprintable:
    def __str__(self) -> str:
        raise ValueError("cannot render")


class TestPublishWarnings:
    """The server reports fields it could not bind; the hook log must show them.

    Silent field-dropping is the defect this channel exists for: a publish of
    50 mis-keyed interactions returned 200 and stored 50 empty rows. This
    plugin's raw ``_make_request`` path matters most — it posts the payload
    directly, so unknown keys really do reach the server rather than being
    stripped client-side first.
    """

    @staticmethod
    def _adapter_records(caplog):
        return [r for r in caplog.records if r.name == _ADAPTER_LOGGER]

    def test_client_path_logs_server_warnings(self, caplog) -> None:
        client = _WarningClient(
            response=SimpleNamespace(
                warnings=["interaction_data_list[0]: ignored unrecognised field(s) Content"],
                request_id="r1",
            )
        )
        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            result = _adapter_with(client).publish(
                session_id="s1",
                project_id="p1",
                interactions=[{"role": "User", "content": "hi"}],
            )
        assert result.ok is True
        assert "unrecognised field(s) Content" in caplog.text

    def test_raw_request_path_logs_server_warnings(self, caplog) -> None:
        """The pinned-request_id path returns parsed JSON, not a model."""
        client = _WarningClient(raw_response={"warnings": ["dropped foo"]})
        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            result = _adapter_with(client).publish(
                session_id="s1",
                project_id="p1",
                request_id="request-1",
                interactions=[{"role": "User", "content": "hi"}],
            )
        assert result.ok is True
        assert result.request_id == "request-1"
        assert "dropped foo" in caplog.text

    def test_quiet_when_there_is_nothing_to_report(self, caplog) -> None:
        client = _WarningClient(response=SimpleNamespace(warnings=[]))
        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            result = _adapter_with(client).publish(
                session_id="s1",
                project_id="p1",
                interactions=[{"role": "User", "content": "hi"}],
            )
        assert result.ok is True
        assert self._adapter_records(caplog) == []

    @pytest.mark.parametrize(
        "response",
        [
            None,
            SimpleNamespace(),
            {"warnings": None},
            {"warnings": 5},
            _WarningsPropertyRaises(),
            _MappingGetRaises(),
            {"warnings": [_Unprintable()]},
        ],
    )
    def test_hostile_response_shapes_still_report_success(self, response) -> None:
        """A publish the server ACCEPTED must never be reported as failed.

        ``publish_unpublished`` advances the buffer watermark only on a truthy
        result, so raising while reading diagnostics would re-send an accepted
        batch on every subsequent hook — duplicates forever, caused purely by
        the code meant to improve observability. The last three shapes are the
        ones that actually escape the helper's isinstance guard.
        """
        client = _WarningClient(response=response, raw_response=response)
        result = _adapter_with(client).publish(
            session_id="s1",
            project_id="p1",
            interactions=[{"role": "User", "content": "hi"}],
        )
        assert result.ok is True
