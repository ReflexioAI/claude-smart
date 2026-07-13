"""Tests for the model-callable learning search data plane."""

from __future__ import annotations

from typing import Any

from claude_smart import learnings_search


class _Adapter:
    def __init__(
        self,
        *,
        playbooks: list[dict[str, Any]] | None = None,
        agent_playbooks: list[dict[str, Any]] | None = None,
        profiles: list[dict[str, Any]] | None = None,
        read_errors: list[str] | None = None,
    ) -> None:
        self.playbooks = playbooks or []
        self.agent_playbooks = agent_playbooks or []
        self.profiles = profiles or []
        self.read_errors = read_errors or []
        self.calls: list[dict[str, Any]] = []

    def search_all(self, **kwargs):
        self.calls.append(kwargs)
        return self.playbooks, self.agent_playbooks, self.profiles


def test_search_learnings_passes_query_project_top_k_and_no_session(
    monkeypatch,
) -> None:
    monkeypatch.setattr(learnings_search.ids, "resolve_user_id", lambda _cwd: "demo")
    adapter = _Adapter(
        playbooks=[{"content": "use uv run", "user_playbook_id": "abc123"}],
        profiles=[{"content": "prefers concise summaries", "profile_id": "pref456"}],
    )

    markdown = learnings_search.search_learnings(
        query=" how to run tests ",
        cwd="/repo/demo",
        top_k=99,
        adapter=adapter,
    )

    assert "project `demo`" in markdown
    assert "use uv run" in markdown
    assert "prefers concise summaries" in markdown
    # Real-id dashboard routes, resolvable without the session registry.
    assert "http://localhost:3001/skills/project/abc123" in markdown
    assert "http://localhost:3001/preferences/project/pref456" in markdown
    assert adapter.calls == [
        {
            "project_id": "demo",
            "query": "how to run tests",
            "top_k": 10,
            "session_id": None,
        }
    ]


def test_search_learnings_output_is_not_citation_tracked(monkeypatch) -> None:
    """MCP hits never enter the per-session citation registry, so the output
    must not instruct the model to emit citation markers the Stop hook could
    never resolve (no ``[cs:…]`` ids, no rank-based ``/rules/`` URLs)."""
    monkeypatch.setattr(learnings_search.ids, "resolve_user_id", lambda _cwd: "demo")
    adapter = _Adapter(
        playbooks=[{"content": "use uv run", "user_playbook_id": "abc123"}],
        profiles=[{"content": "prefers concise summaries", "profile_id": "pref456"}],
    )

    markdown = learnings_search.search_learnings(
        query="how to run tests",
        cwd="/repo/demo",
        adapter=adapter,
    )

    assert "[cs:" not in markdown
    assert "/rules/" not in markdown
    assert "When to cite:" not in markdown
    assert "claude-smart rule applied" not in markdown


def test_search_learnings_requires_absolute_cwd() -> None:
    adapter = _Adapter(playbooks=[{"content": "should not be searched"}])

    markdown = learnings_search.search_learnings(
        query="q",
        cwd="relative/path",
        adapter=adapter,
    )

    assert "absolute `cwd`" in markdown
    assert adapter.calls == []


def test_search_learnings_empty_query_returns_soft_result() -> None:
    adapter = _Adapter(playbooks=[{"content": "should not be searched"}])

    markdown = learnings_search.search_learnings(
        query="   ",
        cwd="/repo/demo",
        adapter=adapter,
    )

    assert "No search query was provided" in markdown
    assert adapter.calls == []


def test_search_learnings_empty_results_are_distinct_from_backend_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(learnings_search.ids, "resolve_user_id", lambda _cwd: "demo")
    adapter = _Adapter()

    markdown = learnings_search.search_learnings(
        query="missing",
        cwd="/repo/demo",
        adapter=adapter,
    )

    assert markdown == "No relevant claude-smart learnings found for project `demo`."


def test_search_learnings_reports_backend_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(learnings_search.ids, "resolve_user_id", lambda _cwd: "demo")
    adapter = _Adapter(read_errors=["unified search: connection refused"])

    markdown = learnings_search.search_learnings(
        query="q",
        cwd="/repo/demo",
        adapter=adapter,
    )

    assert "claude-smart search is unavailable" in markdown
    assert "unified search: connection refused" in markdown


def test_search_learnings_defaults_bad_top_k(monkeypatch) -> None:
    monkeypatch.setattr(learnings_search.ids, "resolve_user_id", lambda _cwd: "demo")
    adapter = _Adapter()

    learnings_search.search_learnings(
        query="q",
        cwd="/repo/demo",
        top_k="not-int",  # type: ignore[arg-type]
        adapter=adapter,
    )

    assert adapter.calls[0]["top_k"] == 5
