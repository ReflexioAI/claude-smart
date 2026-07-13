"""MCP server exposing claude-smart learning search."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from claude_smart.learnings_search import search_learnings as _search_learnings

_TOOL_DESCRIPTION = """Search claude-smart learnings: previous user corrections, project-specific skills, shared skills, and project preferences.

Use before non-trivial coding, debugging, planning, or repository work, and again when the task changes topic. Rewrite `query` into the actual question or execution path you need help with; do not blindly pass the raw user prompt. Always pass the active repo/workspace absolute path as `cwd` so project-scoped memory is correct. Skip only for trivial one-shot questions."""

mcp = FastMCP(
    "claude-smart learnings",
    instructions=(
        "Use search_learnings to retrieve prior user corrections, preferences, "
        "memories, and optimized execution paths from claude-smart."
    ),
)


@mcp.tool(
    name="search_learnings",
    description=_TOOL_DESCRIPTION,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def search_learnings(query: str, cwd: str, top_k: int = 5) -> str:
    """Search claude-smart learnings for the active workspace."""
    return _search_learnings(query=query, cwd=cwd, top_k=top_k)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
