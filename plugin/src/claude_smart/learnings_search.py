"""Search claude-smart learnings for model-callable MCP tools."""

from __future__ import annotations

from pathlib import Path

from claude_smart import context_format, ids
from claude_smart.reflexio_adapter import Adapter

_DEFAULT_TOP_K = 5
_MAX_TOP_K = 10


def search_learnings(
    *,
    query: str,
    cwd: str,
    top_k: int = _DEFAULT_TOP_K,
    adapter: Adapter | None = None,
) -> str:
    """Return markdown learnings for a task-specific search query.

    Args:
        query: Standalone search query rewritten from the current task context.
        cwd: Absolute repo/workspace path used to resolve project-scoped memory.
        top_k: Maximum results per entity type; clamped to ``1..10``.
        adapter: Optional adapter injection seam for tests.

    Returns:
        Markdown text suitable for direct MCP tool output. The function never
        raises for invalid input or an unavailable backend; those cases return a
        short, model-readable instruction/result instead.
    """
    normalized_query = query.strip()
    if not normalized_query:
        return "No search query was provided. Retry `search_learnings` with a concise, task-specific query."

    cwd_path = Path(cwd).expanduser() if cwd else None
    if cwd_path is None or not cwd_path.is_absolute():
        return (
            "search_learnings needs an absolute `cwd` for the active repo/workspace "
            "so claude-smart can search the correct project memory. Retry with the "
            "current workspace path."
        )

    project_id = ids.resolve_user_id(str(cwd_path))
    search_adapter = adapter or Adapter()
    user_playbooks, agent_playbooks, profiles = search_adapter.search_all(
        project_id=project_id,
        query=normalized_query,
        top_k=_normalize_top_k(top_k),
        session_id=None,
    )
    # Plain render: MCP output is not tracked in the per-session citation
    # registry, so citation ids/instructions would produce markers the Stop
    # hook can never resolve.
    markdown = context_format.render_learnings_plain(
        project_id=project_id,
        user_playbooks=user_playbooks,
        agent_playbooks=agent_playbooks,
        profiles=profiles,
    )
    if markdown:
        return f"## claude-smart search_learnings — project `{project_id}`\n{markdown}"

    if search_adapter.read_errors:
        latest = search_adapter.read_errors[-1]
        return (
            f"claude-smart search is unavailable for project `{project_id}`: {latest}. "
            "Proceed normally with the current context."
        )
    return f"No relevant claude-smart learnings found for project `{project_id}`."


def _normalize_top_k(top_k: int) -> int:
    try:
        parsed = int(top_k)
    except (TypeError, ValueError):
        return _DEFAULT_TOP_K
    return max(1, min(parsed, _MAX_TOP_K))
