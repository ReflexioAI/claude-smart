"""Render reflexio preferences + skills as markdown for display or injection."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from claude_smart import cs_cite

_DASHBOARD_URL_ENV = "CLAUDE_SMART_DASHBOARD_URL"
_CITATION_LINK_STYLE_ENV = "CLAUDE_SMART_CITATION_LINK_STYLE"
_DEFAULT_DASHBOARD_URL = "http://localhost:3001"


def _first_nonempty(*values: Any) -> str:
    """Return the first truthy string value, or an empty string."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def render(
    *,
    project_id: str,
    user_playbooks: Iterable[Any],
    agent_playbooks: Iterable[Any],
    profiles: Iterable[Any],
) -> str:
    """Render skills + preferences as full audit markdown.

    Empty sections are omitted. When all sections are empty, returns "".

    Args:
        project_id (str): Displayed in the header so the user can tell
            which project is in effect.
        user_playbooks (Iterable[Any]): Skill records scoped to this project
            (``UserPlaybook`` objects or dicts with the same fields).
        agent_playbooks (Iterable[Any]): Skill records shared across projects
            (``AgentPlaybook`` objects or dicts with the same fields).
        profiles (Iterable[Any]): Iterable of preference records
            (``UserProfile`` objects or dicts).

    Returns:
        str: Markdown, or "" when there is nothing to show.
    """
    markdown, _ = render_with_registry(
        project_id=project_id,
        user_playbooks=user_playbooks,
        agent_playbooks=agent_playbooks,
        profiles=profiles,
    )
    return markdown


def render_with_registry(
    *,
    project_id: str,
    user_playbooks: Iterable[Any],
    agent_playbooks: Iterable[Any],
    profiles: Iterable[Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Variant of ``render`` that also returns the citation registry.

    Every skill and preference bullet is tagged with a short ``[cs:ID]``
    prefix. The registry maps those ids back to ``{id, kind, title,
    content}`` entries so ``events.stop`` can resolve citations into
    human-readable titles for the dashboard.

    Agent playbooks (cross-project, distilled) are listed before user
    playbooks (this project's lessons) under one ``### Project-specific
    skills`` heading. The model doesn't need to reason about the split.

    Args:
        project_id (str): Displayed in the header so the user can tell
            which project is in effect.
        user_playbooks (Iterable[Any]): Skill records scoped to this project.
        agent_playbooks (Iterable[Any]): Skill records shared across projects.
        profiles (Iterable[Any]): Iterable of preference records
            (``UserProfile`` objects or dicts).

    Returns:
        tuple[str, list[dict[str, Any]]]: ``(markdown, registry_entries)``.
            When all input lists are empty the markdown is ``""`` and the
            registry is ``[]``.
    """
    playbook_lines, playbook_entries = _format_combined_playbooks(
        agent_playbooks=agent_playbooks, user_playbooks=user_playbooks
    )
    profile_lines, profile_entries = _format_profiles(profiles)
    if not playbook_lines and not profile_lines:
        return "", []

    sections: list[str] = [f"## claude-smart — project `{project_id}`"]
    if playbook_lines:
        sections.append("### Project-specific skills")
        sections.extend(playbook_lines)
    if profile_lines:
        sections.append("### Project preferences")
        sections.extend(profile_lines)
    instruction = cs_cite.citation_instruction(
        os.environ.get("CLAUDE_SMART_CITATIONS", "on"),
        os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown"),
    )
    if instruction:
        sections.append(instruction)
    return "\n".join(sections) + "\n", playbook_entries + profile_entries


def render_inline(
    *,
    project_id: str,
    user_playbooks: Iterable[Any],
    agent_playbooks: Iterable[Any],
    profiles: Iterable[Any],
) -> str:
    """Render skills + preferences for mid-session injection.

    Same bullet format as ``render`` but with no top-level project header.
    This block is injected just-in-time alongside an in-flight user prompt or
    tool call, so the caller already has project context.

    Args:
        project_id (str): Reserved for future use; currently unused.
        user_playbooks (Iterable[Any]): Relevance-ranked project-scoped hits.
        agent_playbooks (Iterable[Any]): Relevance-ranked global hits.
        profiles (Iterable[Any]): Relevance-ranked preference hits.

    Returns:
        str: Markdown with ``### Relevant project-specific skills`` and/or
            ``### Relevant project preferences`` sub-sections, or ``""``
            when all inputs are empty.
    """
    markdown, _ = render_inline_with_registry(
        project_id=project_id,
        user_playbooks=user_playbooks,
        agent_playbooks=agent_playbooks,
        profiles=profiles,
    )
    return markdown


def render_inline_with_registry(
    *,
    project_id: str,
    user_playbooks: Iterable[Any],
    agent_playbooks: Iterable[Any],
    profiles: Iterable[Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Variant of ``render_inline`` that also returns the citation registry.

    Args:
        project_id (str): Reserved for future use; currently unused.
        user_playbooks (Iterable[Any]): Relevance-ranked project-scoped hits.
        agent_playbooks (Iterable[Any]): Relevance-ranked global hits.
        profiles (Iterable[Any]): Relevance-ranked preference hits.

    Returns:
        tuple[str, list[dict[str, Any]]]: ``(markdown, registry_entries)``.
            When all input lists are empty the markdown is ``""`` and the
            registry is ``[]``.
    """
    del project_id  # kept for symmetry with ``render_with_registry``.
    playbook_lines, playbook_entries = _format_combined_playbooks(
        agent_playbooks=agent_playbooks, user_playbooks=user_playbooks
    )
    profile_lines, profile_entries = _format_profiles(profiles)
    if not playbook_lines and not profile_lines:
        return "", []
    sections: list[str] = []
    if playbook_lines:
        sections.append("### Relevant project-specific skills")
        sections.extend(playbook_lines)
    if profile_lines:
        sections.append("### Relevant project preferences")
        sections.extend(profile_lines)
    instruction = cs_cite.citation_instruction(
        os.environ.get("CLAUDE_SMART_CITATIONS", "on"),
        os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown"),
    )
    if instruction:
        sections.append(instruction)
    return "\n".join(sections) + "\n", playbook_entries + profile_entries


def render_inline_compact_with_registry(
    *,
    project_id: str,
    user_playbooks: Iterable[Any],
    agent_playbooks: Iterable[Any],
    profiles: Iterable[Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Render mid-session context as one compact logical line.

    Codex currently displays ``UserPromptSubmit.additionalContext`` in the TUI.
    This renderer preserves the model-visible ids and rule URLs needed for
    citation tracking while avoiding the multi-line markdown block used by
    hosts that can hide hook context.
    """
    del project_id  # kept for symmetry with ``render_inline_with_registry``.
    _, playbook_entries = _format_combined_playbooks(
        agent_playbooks=agent_playbooks, user_playbooks=user_playbooks
    )
    _, profile_entries = _format_profiles(profiles)
    entries = playbook_entries + profile_entries
    if not entries:
        return "", []

    item_parts = []
    marker_parts = []
    link_style = os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown")
    for entry in entries:
        title = _one_line(str(entry.get("title") or entry["content"]))
        content = _one_line(str(entry["content"]))
        rule_url = str(entry.get("rule_url") or "")
        if link_style == "osc8" and rule_url:
            linked_title = _osc8_link(rule_url, _strip_trailing_sentence_punctuation(title))
            item = linked_title
            if content != title:
                item += f": {content}"
            marker_parts.append(f"✨ claude-smart rule applied: {linked_title}")
        else:
            item = f"{content} (title: {title}"
            if rule_url:
                item += f"; open: {rule_url}"
            item += ")"
        item_parts.append(item)

    sections = [f"claude-smart: using relevant memory: {'; '.join(item_parts)}."]
    instruction = _compact_citation_instruction(marker_parts)
    if instruction:
        sections.append(instruction)
    return " ".join(sections) + "\n", entries


def _compact_citation_instruction(marker_parts: list[str] | None = None) -> str:
    if os.environ.get("CLAUDE_SMART_CITATIONS", "on") == "off":
        return ""
    link_style = os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown")
    if link_style == "osc8" and marker_parts:
        marker = marker_parts[0] if len(marker_parts) == 1 else "; ".join(marker_parts)
        return (
            f"If used, copy this final marker exactly, preserving its hidden "
            f"OSC 8 terminal link: `{marker}`. Skip when unrelated."
        )
    if link_style == "osc8":
        return (
            "If used, end with `✨ claude-smart rule applied:` followed by "
            "the same linked memory text; keep the link, but do not show the "
            "URL. Skip when unrelated."
        )
    return (
        "Only if a listed [cs:...] item materially changes your answer, end "
        "with one final marker like `✨ claude-smart rule applied: "
        "[verify process state](http://localhost:3001/rules/s1-123)` using "
        "the shown rule URL; skip the marker when unrelated."
    )


def _format_combined_playbooks(
    *,
    agent_playbooks: Iterable[Any],
    user_playbooks: Iterable[Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Render agent then user playbooks with one shared rank counter."""
    lines: list[str] = []
    entries: list[dict[str, Any]] = []
    rank = 0
    for pb in agent_playbooks:
        rank = _append_playbook_bullet(
            pb, "agent_playbook_id", "agent_playbook", rank, lines, entries
        )
    for pb in user_playbooks:
        rank = _append_playbook_bullet(
            pb, "user_playbook_id", "user_playbook", rank, lines, entries
        )
    return lines, entries


def _append_playbook_bullet(
    pb: Any,
    id_field: str,
    source_kind: str,
    rank: int,
    lines: list[str],
    entries: list[dict[str, Any]],
) -> int:
    content = _first_nonempty(_field(pb, "content"))
    if not content:
        return rank
    rank += 1
    trigger = _first_nonempty(_field(pb, "trigger"))
    rationale = _first_nonempty(_field(pb, "rationale"))
    real_id = _field(pb, id_field)
    item_id = cs_cite.rank_id("playbook", rank, real_id)
    title = _title_from_content(content)
    dashboard_url = _dashboard_url("playbook", real_id, source_kind)
    rule_url = _rule_url(item_id)
    bullet = f"- [cs:{item_id}] {content}"
    if trigger:
        bullet += f" _(when: {trigger})_"
    if rationale:
        bullet += f" — *why:* {rationale}"
    if rule_url:
        bullet += f" _(open: {rule_url})_"
    lines.append(bullet)
    entries.append(
        {
            "id": item_id,
            "kind": "playbook",
            "title": title,
            "content": content,
            "real_id": str(real_id) if real_id is not None else None,
            "source_kind": source_kind,
            "dashboard_url": dashboard_url,
            "rule_url": rule_url,
        }
    )
    return rank


def _format_profiles(
    profiles: Iterable[Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    lines: list[str] = []
    entries: list[dict[str, Any]] = []
    rank = 0
    for p in profiles:
        content = _first_nonempty(_field(p, "content"))
        if not content:
            continue
        rank += 1
        real_id = _field(p, "profile_id")
        item_id = cs_cite.rank_id("profile", rank, real_id)
        title = _title_from_content(content)
        dashboard_url = _dashboard_url("profile", real_id)
        rule_url = _rule_url(item_id)
        bullet = f"- [cs:{item_id}] {content}"
        if rule_url:
            bullet += f" _(open: {rule_url})_"
        lines.append(bullet)
        entries.append(
            {
                "id": item_id,
                "kind": "profile",
                "title": title,
                "content": content,
                "real_id": str(real_id) if real_id is not None else None,
                "dashboard_url": dashboard_url,
                "rule_url": rule_url,
            }
        )
    return lines, entries


def _dashboard_url(kind: str, real_id: Any, source_kind: str | None = None) -> str:
    if real_id is None:
        return ""
    encoded_id = quote(str(real_id), safe="")
    base = os.environ.get(_DASHBOARD_URL_ENV, _DEFAULT_DASHBOARD_URL).rstrip("/")
    if kind == "profile":
        return f"{base}/preferences/project/{encoded_id}"
    if kind == "playbook":
        skill_kind = "shared" if source_kind == "agent_playbook" else "project"
        return f"{base}/skills/{skill_kind}/{encoded_id}"
    return ""


def _rule_url(item_id: str) -> str:
    if not item_id:
        return ""
    encoded_id = quote(item_id, safe="")
    base = os.environ.get(_DASHBOARD_URL_ENV, _DEFAULT_DASHBOARD_URL).rstrip("/")
    return f"{base}/rules/{encoded_id}"


def _title_from_content(content: str, limit: int = 80) -> str:
    """Derive a compact human-readable title from a bullet's content.

    Truncates at the first sentence boundary when one falls within the
    character limit; otherwise hard-trims with an ellipsis. Used only for
    dashboard display.
    """
    text = content.strip()
    if not text:
        return ""
    for terminator in (". ", "\n"):
        idx = text.find(terminator)
        if 0 < idx <= limit:
            return text[:idx].rstrip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _osc8_link(url: str, label: str) -> str:
    return f"\x1b]8;;{url}\x1b\\{label}\x1b]8;;\x1b\\"


def _strip_trailing_sentence_punctuation(text: str) -> str:
    return text.rstrip().rstrip(".!?")


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from either an attribute or a dict key."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
