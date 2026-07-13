"""Tests for the claude-smart MCP server adapter."""

from __future__ import annotations

from claude_smart import mcp_server


def test_search_learnings_tool_delegates_to_shared_search(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(mcp_server, "_search_learnings", fake_search)

    assert mcp_server.search_learnings("question", "/repo/demo", 7) == "ok"
    assert calls == [{"query": "question", "cwd": "/repo/demo", "top_k": 7}]


def test_search_learnings_tool_uses_default_top_k(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(mcp_server, "_search_learnings", fake_search)

    assert mcp_server.search_learnings("question", "/repo/demo") == "ok"
    assert calls[0]["top_k"] == 5
