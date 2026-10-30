"""Render reflexio preferences + skills as markdown for display or injection."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote, urlparse

from claude_smart import cs_cite, env_config

_DASHBOARD_URL_ENV = "CLAUDE_SMART_DASHBOARD_URL"
_CITATION_LINK_STYLE_ENV = "CLAUDE_SMART_CITATION_LINK_STYLE"
_DEFAULT_DASHBOARD_URL = "http://localhost:3001"
_REFLEXIO_URL_ENV = "REFLEXIO_URL"
_LOCAL_REFLEXIO_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}  # noqa: S104 -- Host allowlist; no socket is bound.

# Claude Code caps each hook ``additionalContext`` string at 10,000 characters.
# A longer one is saved to a file and the model sees only a 2,000-character
# preview. Index lines are bounded, so this budget is a backstop: overflow
# drops only the lowest-ranked lines. The 2,000-character margin absorbs URL
# and instruction growth without a re-measure.
INLINE_CONTEXT_BUDGET_CHARS = 8_000

# Codex limits each model-visible hook-output message to "roughly 2,500
# tokens" (``additionalContextLimit``) and saves longer output to a file.
# 6,000 characters stays under that even at 2.4 characters per token, which
# URL-heavy index lines do not reach. OpenCode documents no limit and gets
# the Claude Code budget.
CODEX_INLINE_CONTEXT_BUDGET_CHARS = 6_000

# Upper bound for the "when it applies" text on one index line.
INDEX_LABEL_MAX_CHARS = 150

# Shell command that prints an injected item's full text (``cli.py load``).
# ``~/.reflexio/plugin-root`` is the symlink ``ensure-plugin-root.sh``
# refreshes on every SessionStart for both Claude Code and Codex, and
# ``cli.sh`` bootstraps PATH and the plugin venv, so the model can run this
# from any shell either host gives it.
LOADER_COMMAND = "bash ~/.reflexio/plugin-root/scripts/cli.sh load"

# Upper bound for a citation-marker link label shown in the footer.
_MARKER_TITLE_MAX_CHARS = 60


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
    instruction = _citation_instruction(
        os.environ.get("CLAUDE_SMART_CITATIONS", "on"),
        os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown"),
    )
    if instruction:
        sections.append(instruction)
    exact_osc8_marker = _exact_osc8_marker_instruction(
        playbook_entries + profile_entries
    )
    if exact_osc8_marker:
        sections.append(exact_osc8_marker)
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


def inline_budget_chars(host: str) -> int:
    """Return the inline context budget for ``host`` (see the constants)."""
    if host == "codex":
        return CODEX_INLINE_CONTEXT_BUDGET_CHARS
    return INLINE_CONTEXT_BUDGET_CHARS


def render_inline_with_registry(
    *,
    project_id: str,
    user_playbooks: Iterable[Any],
    agent_playbooks: Iterable[Any],
    profiles: Iterable[Any],
    session_id: str = "",
    budget_chars: int = INLINE_CONTEXT_BUDGET_CHARS,
) -> tuple[str, list[dict[str, Any]]]:
    """Render the mid-session index block and its citation registry.

    Like a skill list, each retrieved item is one line: its ``[cs:ID]``,
    when it applies (its ``trigger``, or the first line of its content when
    it has none), and its link. A header tells the model how to load an
    item's full text with ``LOADER_COMMAND``. The block never exceeds
    ``budget_chars``; the registry lists only the lines kept.

    Args:
        project_id (str): Reserved for future use; currently unused.
        user_playbooks (Iterable[Any]): Relevance-ranked project-scoped hits.
        agent_playbooks (Iterable[Any]): Relevance-ranked global hits.
        profiles (Iterable[Any]): Relevance-ranked preference hits.
        session_id (str): Session whose registry the loader should read.
            Omitted from the load command when empty.
        budget_chars (int): Size cap for the whole block; see
            ``inline_budget_chars``.

    Returns:
        tuple[str, list[dict[str, Any]]]: ``(markdown, registry_entries)``.
            When all input lists are empty the markdown is ``""`` and the
            registry is ``[]``.
    """
    del project_id  # kept for symmetry with ``render_with_registry``.
    _, playbook_entries = _format_combined_playbooks(
        agent_playbooks=agent_playbooks, user_playbooks=user_playbooks
    )
    _, profile_entries = _format_profiles(profiles)
    return _fit_inline_budget(
        playbook_entries,
        profile_entries,
        lambda playbooks, prefs: _assemble_index(playbooks, prefs, session_id),
        budget_chars,
    )


def render_inline_compact_with_registry(
    *,
    project_id: str,
    user_playbooks: Iterable[Any],
    agent_playbooks: Iterable[Any],
    profiles: Iterable[Any],
    session_id: str = "",
    budget_chars: int = CODEX_INLINE_CONTEXT_BUDGET_CHARS,
) -> tuple[str, list[dict[str, Any]]]:
    """Render the same index as one logical line, for Codex.

    Codex currently displays ``UserPromptSubmit.additionalContext`` in the
    TUI, so the index entries are joined on one line instead of listed.
    """
    del project_id  # kept for symmetry with ``render_inline_with_registry``.
    _, playbook_entries = _format_combined_playbooks(
        agent_playbooks=agent_playbooks, user_playbooks=user_playbooks
    )
    _, profile_entries = _format_profiles(profiles)
    return _fit_inline_budget(
        playbook_entries,
        profile_entries,
        lambda playbooks, prefs: _assemble_compact(playbooks, prefs, session_id),
        budget_chars,
    )


def render_loaded_item(entry: dict[str, Any]) -> str:
    """Render one registry entry's full text for ``cli.py load``."""
    kind = entry.get("kind")
    if kind == "profile":
        label = "preference"
    elif entry.get("source_kind") == "agent_playbook":
        label = "shared skill"
    else:
        label = "project skill"
    lines = [f"[cs:{entry.get('id', '')}] {label}"]
    trigger = _first_nonempty(entry.get("trigger"))
    if trigger:
        lines.append(f"When: {trigger}")
    lines.append(str(entry.get("content") or "").strip())
    rationale = _first_nonempty(entry.get("rationale"))
    if rationale:
        lines.append(f"Why: {rationale}")
    rule_url = _first_nonempty(entry.get("rule_url"))
    if rule_url:
        lines.append(f"Open: {rule_url}")
    return "\n".join(lines) + "\n"


def _fit_inline_budget(
    playbooks: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    assemble: Callable[[list[dict[str, Any]], list[dict[str, Any]]], str],
    budget_chars: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Keep the highest-ranked index lines whose whole block fits the budget.

    Lines are admitted by rank, alternating sections (skill 1, preference 1,
    skill 2, ...), until the next one would push the assembled block, header
    and footer included, past ``budget_chars``. Every line is
    bounded, so only the lowest-ranked lines are ever dropped.

    Returns:
        tuple[str, list[dict[str, Any]]]: ``(text, registry_entries)`` for
            the lines kept, or ``("", [])`` when there are no items or none
            fits; the header and footer are never emitted on their own.
    """
    order = _interleave_by_rank(len(playbooks), len(profiles))
    text = ""
    kept_playbooks = kept_profiles = 0
    for keep in range(1, len(order) + 1):
        playbook_count = order[:keep].count("playbook")
        trial = assemble(playbooks[:playbook_count], profiles[: keep - playbook_count])
        if len(trial) > budget_chars:
            break
        text = trial
        kept_playbooks, kept_profiles = playbook_count, keep - playbook_count
    if not text:
        return "", []
    return text, playbooks[:kept_playbooks] + profiles[:kept_profiles]


def _interleave_by_rank(playbook_count: int, profile_count: int) -> list[str]:
    order: list[str] = []
    for rank in range(max(playbook_count, profile_count)):
        if rank < playbook_count:
            order.append("playbook")
        if rank < profile_count:
            order.append("profile")
    return order


def _assemble_index(
    playbooks: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    session_id: str,
) -> str:
    """Render the multi-line index block for exactly the given entries."""
    sections: list[str] = [_index_header(session_id)]
    if playbooks:
        sections.append("### Relevant project-specific skills")
        sections.extend(_index_line(entry) for entry in playbooks)
    if profiles:
        sections.append("### Relevant project preferences")
        sections.extend(_index_line(entry) for entry in profiles)
    instruction = _citation_instruction(
        os.environ.get("CLAUDE_SMART_CITATIONS", "on"),
        os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown"),
    )
    if instruction:
        sections.append(instruction)
    exact_osc8_marker = _exact_osc8_marker_instruction(playbooks + profiles)
    if exact_osc8_marker:
        sections.append(exact_osc8_marker)
    return "\n".join(sections) + "\n"


def _assemble_compact(
    playbooks: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    session_id: str,
) -> str:
    """Render the one-line index block for exactly the given entries."""
    link_style = os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown")
    marker_parts = []
    for entry in playbooks + profiles:
        rule_url = str(entry.get("rule_url") or "")
        if not rule_url:
            continue
        title = _marker_title(entry)
        marker_parts.append(
            _osc8_link(rule_url, title)
            if link_style == "osc8"
            else _markdown_link(rule_url, title)
        )
    sections = [_index_header(session_id)]
    if playbooks:
        label = "Skill" if len(playbooks) == 1 else "Skills"
        sections.append(
            f"{label}: {'; '.join(_compact_index_item(e) for e in playbooks)}."
        )
    if profiles:
        label = "Preference" if len(profiles) == 1 else "Preferences"
        sections.append(
            f"{label}: {'; '.join(_compact_index_item(e) for e in profiles)}."
        )
    instruction = _compact_citation_instruction(marker_parts)
    if instruction:
        sections.append(instruction)
    return " ".join(sections) + "\n"


def _index_header(session_id: str) -> str:
    command = LOADER_COMMAND
    if session_id:
        command += f" --session {session_id}"
    return (
        "Relevant learnings from claude-smart — each line says when one "
        "applies. Before relying on one that applies, load its full text "
        f"with: `{command} <id>`"
    )


def _index_line(entry: dict[str, Any]) -> str:
    line = f"- [cs:{entry['id']}] {index_label(entry)}"
    rule_url = str(entry.get("rule_url") or "")
    if rule_url:
        line += f" _(open: {rule_url})_"
    return line


def _compact_index_item(entry: dict[str, Any]) -> str:
    label = index_label(entry)
    rule_url = str(entry.get("rule_url") or "")
    if rule_url and os.environ.get(_CITATION_LINK_STYLE_ENV) == "osc8":
        label = _osc8_link(rule_url, label)
    elif rule_url:
        label += f" (open: {rule_url})"
    return f"[cs:{entry['id']}] {label}"


def index_label(entry: dict[str, Any]) -> str:
    """Return the bounded "when it applies" text for one index line.

    Uses the item's ``trigger``; without one (always so for preferences),
    the first line of its content. Cut at a word boundary to
    ``INDEX_LABEL_MAX_CHARS`` plus an ellipsis.
    """
    text = _one_line(str(entry.get("trigger") or ""))
    if not text:
        text = _one_line(str(entry.get("content") or "").strip().split("\n", 1)[0])
    return _cut_at_word(text, INDEX_LABEL_MAX_CHARS)


def _marker_title(entry: dict[str, Any]) -> str:
    """Short link label for citation-marker examples, from the index label.

    Derived from the same text as the index line, so the injected block
    never quotes an item's content when the item has a trigger.
    """
    label = index_label(entry).removesuffix("…")
    sentence_end = label.find(". ")
    if 0 < sentence_end <= _MARKER_TITLE_MAX_CHARS:
        label = label[:sentence_end]
    title = _cut_at_word(label, _MARKER_TITLE_MAX_CHARS).removesuffix("…")
    return _strip_trailing_sentence_punctuation(title)


def _cut_at_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    cut = head.rsplit(" ", 1)[0] if " " in head else text[:limit]
    return cut.rstrip(" ,;:.") + "…"


def _compact_citation_instruction(marker_parts: list[str] | None = None) -> str:
    if os.environ.get("CLAUDE_SMART_CITATIONS", "on") == "off":
        return ""
    gate = cs_cite.WHEN_TO_CITE_COMPACT
    link_style = os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown")
    if link_style == "osc8" and marker_parts:
        example = cs_cite.build_marker(marker_parts[0], "osc8")
        return _remoteize_citation_instruction(
            f"{gate} If you do, use this final marker format, preserving its "
            f"hidden OSC 8 terminal link: `{example}`. Include only memories "
            "that materially changed the response. Use their links above. "
            "Separate multiple linked memories with the visible ` | ` separator."
        )
    if link_style == "osc8":
        return _remoteize_citation_instruction(
            f"{gate} If you do, end with `{cs_cite.MARKER_PREFIX}` "
            "followed by the same linked memory text, then "
            f"`{cs_cite.marker_attribution('osc8')}` linking to the reflexio "
            "repo; keep the links, but do not show the URL."
        )
    if marker_parts:
        example = cs_cite.build_marker(marker_parts[0], "markdown")
        return _remoteize_citation_instruction(
            f"{gate} If you do, use this final marker format with markdown "
            f"links: `{example}`. Include only memories that materially changed "
            "the response. "
            "Separate multiple linked memories with the visible ` | ` separator."
        )
    return _remoteize_citation_instruction(
        f"{gate} If you do, end with one final marker like "
        f"`{cs_cite.MARKDOWN_EXAMPLE_ONE}` using the shown rule URL."
    )


def _citation_instruction(mode: str, link_style: str) -> str:
    return _remoteize_citation_instruction(
        cs_cite.citation_instruction(mode, link_style)
    )


def _remoteize_citation_instruction(text: str) -> str:
    playbooks_url = _remote_reflexio_page_url("playbook")
    profiles_url = _remote_reflexio_page_url("profile")
    if not text or not playbooks_url or not profiles_url:
        return text
    return text.replace("http://localhost:3001/rules/s1-123", playbooks_url).replace(
        "http://localhost:3001/rules/p1-pref", profiles_url
    )


def _exact_osc8_marker_instruction(entries: list[dict[str, Any]]) -> str:
    if os.environ.get("CLAUDE_SMART_CITATIONS", "on") == "off":
        return ""
    if os.environ.get(_CITATION_LINK_STYLE_ENV, "markdown") != "osc8":
        return ""

    marker_parts = []
    for entry in entries:
        rule_url = str(entry.get("rule_url") or "")
        if not rule_url:
            continue
        marker_parts.append(_osc8_link(rule_url, _marker_title(entry)))
    if not marker_parts:
        return ""

    example = cs_cite.build_marker(marker_parts[0], "osc8")
    available = " · ".join(marker_parts)
    return (
        "If any listed memory above was used, use this final marker format, "
        f"preserving its hidden OSC 8 terminal link: `{example}`. Include only "
        f"memories that materially changed the response. Available linked titles: "
        f"{available}. "
        "Separate multiple linked memories with the visible ` | ` separator."
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
    rule_url = _rule_url(item_id, "playbook", real_id, source_kind)
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
            # Kept so ``cli.py load`` can print the full item from the
            # session registry without a backend round trip.
            "trigger": trigger,
            "rationale": rationale,
        }
    )
    receipt = _field(pb, "exposure_receipt")
    if isinstance(receipt, str) and len(receipt) <= 8_192:
        entries[-1]["exposure_receipt"] = receipt
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
        rule_url = _rule_url(item_id, "profile", real_id)
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
    remote_url = _remote_reflexio_item_url(kind, real_id, source_kind)
    if remote_url:
        return remote_url
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


def _rule_url(
    item_id: str, kind: str, real_id: Any = None, source_kind: str | None = None
) -> str:
    remote_url = _remote_reflexio_item_url(kind, real_id, source_kind)
    if remote_url:
        return remote_url
    if not item_id:
        return ""
    encoded_id = quote(item_id, safe="")
    base = os.environ.get(_DASHBOARD_URL_ENV, _DEFAULT_DASHBOARD_URL).rstrip("/")
    return f"{base}/rules/{encoded_id}"


def _remote_reflexio_item_url(
    kind: str, real_id: Any, source_kind: str | None = None
) -> str:
    origin = _remote_reflexio_origin()
    if not origin:
        return ""
    if real_id is None:
        return _remote_reflexio_page_url(kind)
    encoded_id = quote(str(real_id), safe="")
    if kind == "profile":
        return f"{origin}/profiles?profile_id={encoded_id}"
    if kind == "playbook":
        if source_kind == "user_playbook":
            return (
                f"{origin}/playbooks?resource=user_playbook&"
                f"user_playbook_id={encoded_id}"
            )
        return f"{origin}/playbooks?agent_playbook_id={encoded_id}"
    return ""


def _remote_reflexio_page_url(kind: str) -> str:
    origin = _remote_reflexio_origin()
    if not origin:
        return ""
    if kind == "profile":
        return f"{origin}/profiles"
    if kind == "playbook":
        return f"{origin}/playbooks"
    return ""


def _remote_reflexio_origin() -> str:
    env_config.load_reflexio_env()
    raw = os.environ.get(_REFLEXIO_URL_ENV, "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if (parsed.hostname or "").lower() in _LOCAL_REFLEXIO_HOSTS:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


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


def _markdown_link(url: str, label: str) -> str:
    safe_label = label.replace("[", "\\[").replace("]", "\\]")
    safe_url = url.replace(")", "%29")
    return f"[{safe_label}]({safe_url})"


def _strip_trailing_sentence_punctuation(text: str) -> str:
    return text.rstrip().rstrip(".!?")


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from either an attribute or a dict key."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
