"""Tests for the thin reflexio adapter."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from claude_smart import reflexio_adapter


class _FakeClient:
    """Minimal stand-in for ReflexioClient used in these tests."""

    def __init__(
        self,
        *,
        publish_ok: bool = True,
        playbook_resp=None,
        profile_resp=None,
    ):
        self._publish_ok = publish_ok
        self._playbook_resp = playbook_resp
        self._profile_resp = profile_resp
        self.published_kwargs: dict[str, Any] = {}

    def publish_interaction(self, **kwargs):
        self.published_kwargs = kwargs
        if not self._publish_ok:
            raise RuntimeError("reflexio unreachable")

    def search_user_playbooks(self, **_kw):
        return self._playbook_resp

    def search_user_profiles(self, **_kw):
        return self._profile_resp


def _adapter_with(client) -> reflexio_adapter.Adapter:
    a = reflexio_adapter.Adapter()
    a._client = client  # bypass lazy constructor
    return a


def test_publish_returns_true_on_success() -> None:
    client = _FakeClient()
    a = _adapter_with(client)
    ok = a.publish(
        session_id="s1",
        project_id="p1",
        interactions=[{"role": "User", "content": "hi"}],
        force_extraction=True,
    )
    assert ok is True
    # After the project-scoped-profiles refactor (commit 88cb150), reflexio's
    # ``user_id`` is the project slug and ``session_id`` is sent separately.
    assert client.published_kwargs["user_id"] == "p1"
    assert client.published_kwargs["session_id"] == "s1"
    assert client.published_kwargs["agent_version"] == "p1"
    assert client.published_kwargs["force_extraction"] is True


def test_publish_returns_false_when_client_raises() -> None:
    a = _adapter_with(_FakeClient(publish_ok=False))
    ok = a.publish(
        session_id="s1",
        project_id="p1",
        interactions=[{"role": "User", "content": "hi"}],
        force_extraction=False,
    )
    assert ok is False


def test_publish_trivially_true_when_no_interactions() -> None:
    a = _adapter_with(_FakeClient())
    assert a.publish(session_id="s", project_id="p", interactions=[]) is True


def test_fetch_playbooks_reads_user_playbooks_field() -> None:
    resp = SimpleNamespace(user_playbooks=[{"content": "rule"}])
    a = _adapter_with(_FakeClient(playbook_resp=resp))
    assert a.fetch_playbooks() == [{"content": "rule"}]


def test_fetch_profiles_reads_user_profiles_field() -> None:
    resp = SimpleNamespace(user_profiles=[{"content": "pref"}])
    a = _adapter_with(_FakeClient(profile_resp=resp))
    assert a.fetch_project_profiles("p1") == [{"content": "pref"}]


def test_fetch_helpers_return_empty_on_unknown_shape() -> None:
    a = _adapter_with(_FakeClient(playbook_resp=object(), profile_resp=object()))
    assert a.fetch_playbooks() == []
    assert a.fetch_project_profiles("p1") == []


def test_publish_returns_false_when_client_unavailable(monkeypatch) -> None:
    a = reflexio_adapter.Adapter()
    monkeypatch.setattr(a, "_get_client", lambda: None)
    assert (
        a.publish(
            session_id="s",
            project_id="p",
            interactions=[{"role": "User", "content": "x"}],
        )
        is False
    )


# -----------------------------------------------------------------------------
# Query-aware search
# -----------------------------------------------------------------------------


class _RecordingClient:
    """Captures kwargs of every search call; returns the canned response."""

    def __init__(self, *, playbook_resp=None, profile_resp=None):
        self._playbook_resp = playbook_resp
        self._profile_resp = profile_resp
        self.playbook_kwargs: dict[str, Any] = {}
        self.profile_kwargs: dict[str, Any] = {}

    def search_user_playbooks(self, **kwargs):
        self.playbook_kwargs = kwargs
        return self._playbook_resp

    def search_user_profiles(self, **kwargs):
        self.profile_kwargs = kwargs
        return self._profile_resp


def test_search_playbooks_passes_query_and_hybrid_mode() -> None:
    client = _RecordingClient(
        playbook_resp=SimpleNamespace(user_playbooks=[{"content": "rule"}])
    )
    a = _adapter_with(client)
    result = a.search_playbooks(query="config.toml", top_k=3)
    assert result == [{"content": "rule"}]
    # Playbooks are retrieved globally: no agent_version / user_id filter.
    assert "agent_version" not in client.playbook_kwargs
    assert client.playbook_kwargs["user_id"] is None
    assert client.playbook_kwargs["query"] == "config.toml"
    assert client.playbook_kwargs["top_k"] == 3
    assert client.playbook_kwargs["search_mode"] == "hybrid"
    assert client.playbook_kwargs["status_filter"] == [None]


def test_search_profiles_scopes_to_project_id() -> None:
    """After commit 88cb150 profiles are project-scoped: user_id = project slug."""
    client = _RecordingClient(
        profile_resp=SimpleNamespace(user_profiles=[{"content": "pref"}])
    )
    a = _adapter_with(client)
    assert a.search_profiles(project_id="proj", query="q") == [{"content": "pref"}]
    assert client.profile_kwargs["user_id"] == "proj"
    assert client.profile_kwargs["query"] == "q"


def test_search_both_returns_both_lists() -> None:
    client = _RecordingClient(
        playbook_resp=SimpleNamespace(user_playbooks=[{"content": "r"}]),
        profile_resp=SimpleNamespace(user_profiles=[{"content": "p"}]),
    )
    a = _adapter_with(client)
    playbooks, profiles = a.search_both(project_id="proj", query="q", top_k=2)
    assert playbooks == [{"content": "r"}]
    assert profiles == [{"content": "p"}]
    # Both legs see the same query.
    assert client.playbook_kwargs["query"] == "q"
    assert client.profile_kwargs["query"] == "q"


def test_search_both_runs_legs_in_parallel() -> None:
    """Serial would be ~0.4s; parallel should be ~0.2s."""

    class SlowClient:
        def search_user_playbooks(self, **_kw):
            time.sleep(0.2)
            return SimpleNamespace(user_playbooks=[])

        def search_profiles(self, **_kw):
            time.sleep(0.2)
            return SimpleNamespace(user_profiles=[])

    a = _adapter_with(SlowClient())
    t0 = time.perf_counter()
    a.search_both(project_id="p", query="q")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.35, f"legs did not run in parallel (elapsed={elapsed:.3f}s)"


def test_search_both_absorbs_one_leg_failure() -> None:
    class HalfBroken:
        def search_user_playbooks(self, **_kw):
            raise RuntimeError("playbook search down")

        def search_user_profiles(self, **_kw):
            return SimpleNamespace(user_profiles=[{"content": "p"}])

    a = _adapter_with(HalfBroken())
    playbooks, profiles = a.search_both(project_id="p", query="q")
    assert playbooks == []
    assert profiles == [{"content": "p"}]


def test_fetch_both_parallelizes_broad_fetch() -> None:
    client = _RecordingClient(
        playbook_resp=SimpleNamespace(user_playbooks=[{"content": "r"}]),
        profile_resp=SimpleNamespace(user_profiles=[{"content": "p"}]),
    )
    a = _adapter_with(client)
    playbooks, profiles = a.fetch_both(project_id="proj")
    assert playbooks == [{"content": "r"}]
    assert profiles == [{"content": "p"}]
    # Broad fetch path does NOT set `query` — confirm the empty-query recency fallback is used.
    assert "query" not in client.playbook_kwargs
    assert client.profile_kwargs["query"] == ""


def test_fetch_playbooks_default_top_k_is_tightened() -> None:
    """SessionStart's broad inject used to be 50; narrowed because PreToolUse carries specificity."""
    client = _RecordingClient(playbook_resp=SimpleNamespace(user_playbooks=[]))
    a = _adapter_with(client)
    a.fetch_playbooks()
    assert client.playbook_kwargs["top_k"] == 10
    # Retrieval is global: no agent_version filter.
    assert "agent_version" not in client.playbook_kwargs


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
    assert opt.optimize_user_playbooks is True
    assert opt.optimize_agent_playbooks is False
    assert opt.auto_update_user_playbooks is True
    assert opt.min_commit_windows == 1
    assert opt.max_metric_calls == 5
    assert opt.assistant_script_path == "/venv/bin/assistant"
    assert opt.assistant_script_args == []
    assert opt.webhook_url is None
    assert opt.webhook_timeout_seconds == 300


def test_apply_optimizer_defaults_skips_set_when_values_match() -> None:
    opt = SimpleNamespace(
        enabled=True,
        optimize_user_playbooks=True,
        optimize_agent_playbooks=False,
        auto_update_user_playbooks=True,
        min_commit_windows=1,
        max_metric_calls=5,
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
