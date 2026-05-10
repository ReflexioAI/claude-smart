"""Compose a reflexio search query from a PreToolUse payload.

Deterministic — no LLM call — so the PreToolUse hook can stay inside its
latency budget. The output is fed to ``ReflexioClient.search(query=...)``
(the unified ``/api/search`` endpoint, which fans out to user playbooks,
agent playbooks, and preferences server-side), which tokenizes via reflexio's
FTS5 sanitizer (OR-joined, stemmed) plus a vector-similarity leg. Short,
meaning-dense strings give the most selective hybrid ranking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

_MAX_SNIPPET_LEN = 400


def from_tool_call(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    """Compose a search query from a Claude Code PreToolUse payload.

    Args:
        tool_name (str): Claude Code tool name (e.g. ``"Edit"``, ``"Bash"``).
        tool_input (Mapping[str, Any]): The tool's input dict as delivered
            by the hook payload.

    Returns:
        str: A short query suitable for reflexio hybrid search, or ``""``
            when the tool is not one we compose for (caller should then
            skip the search entirely).
    """
    match tool_name:
        case "Edit" | "Write" | "NotebookEdit":
            return _from_file_edit(tool_input)
        case "Bash":
            return _from_bash(tool_input)
        case _:
            return ""


def _from_file_edit(tool_input: Mapping[str, Any]) -> str:
    path = tool_input.get("file_path") or ""
    snippet = tool_input.get("new_string") or tool_input.get("content") or ""
    basename = Path(path).name if path else ""
    return f"{basename} {snippet[:_MAX_SNIPPET_LEN]}".strip()


def _from_bash(tool_input: Mapping[str, Any]) -> str:
    command = tool_input.get("command") or ""
    first_line = command.splitlines()[0] if command else ""
    return first_line[:_MAX_SNIPPET_LEN].strip()
