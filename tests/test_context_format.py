"""Tests for the context-markdown renderer and citation registry."""

from __future__ import annotations

import re

import pytest
from claude_smart import context_format, cs_cite, runtime


def test_render_with_registry_empty_returns_empty_tuple() -> None:
    md, registry = context_format.render_with_registry(
        project_id="demo", user_playbooks=[], agent_playbooks=[], profiles=[]
    )
    assert md == ""
    assert registry == []


def test_render_with_registry_empty_content_items_ignored() -> None:
    """Items whose ``content`` is blank contribute neither bullet nor registry entry."""
    md, registry = context_format.render_with_registry(
        project_id="demo",
        user_playbooks=[{"content": ""}, {"content": "   "}],
        agent_playbooks=[{"content": None}],
        profiles=[{"content": None}],
    )
    assert md == ""
    assert registry == []


def test_render_with_registry_ids_match_between_markdown_and_registry() -> None:
    pbs = [{"content": "use pathlib", "user_playbook_id": 17}]
    prs = [{"content": "prefers anyio", "profile_id": "uuid-profile-1"}]
    md, registry = context_format.render_with_registry(
        project_id="demo", user_playbooks=pbs, agent_playbooks=[], profiles=prs
    )
    assert "[cs:s1-17]" in md
    assert "[cs:p1-uuid]" in md
    assert {e["id"] for e in registry} == {"s1-17", "p1-uuid"}
    by_id = {e["id"]: e for e in registry}
    assert by_id["s1-17"]["kind"] == "playbook"
    assert by_id["p1-uuid"]["kind"] == "profile"
    assert by_id["s1-17"]["real_id"] == "17"
    assert by_id["p1-uuid"]["real_id"] == "uuid-profile-1"
    assert by_id["s1-17"]["dashboard_url"] == "http://localhost:3001/skills/project/17"
    assert by_id["s1-17"]["rule_url"] == "http://localhost:3001/rules/s1-17"
    assert (
        by_id["p1-uuid"]["dashboard_url"]
        == "http://localhost:3001/preferences/project/uuid-profile-1"
    )
    assert by_id["p1-uuid"]["rule_url"] == "http://localhost:3001/rules/p1-uuid"


def test_render_with_registry_ranks_increase_in_order() -> None:
    """Rank ids reflect retrieval order within each kind."""
    pbs = [
        {"content": "first rule", "user_playbook_id": 1},
        {"content": "second rule", "user_playbook_id": 2},
    ]
    prs = [
        {"content": "first pref", "profile_id": "a"},
        {"content": "second pref", "profile_id": "b"},
    ]
    md, registry = context_format.render_with_registry(
        project_id="demo", user_playbooks=pbs, agent_playbooks=[], profiles=prs
    )
    assert "[cs:s1-1] first rule" in md
    assert "[cs:s2-2] second rule" in md
    assert "[cs:p1-a] first pref" in md
    assert "[cs:p2-b] second pref" in md
    assert [e["id"] for e in registry] == ["s1-1", "s2-2", "p1-a", "p2-b"]


def test_render_with_registry_omits_fingerprint_when_real_id_missing() -> None:
    """Items without a real id render as bare ranks (back-compat path)."""
    pbs = [{"content": "orphan rule"}]
    prs = [{"content": "orphan pref"}]
    md, registry = context_format.render_with_registry(
        project_id="demo", user_playbooks=pbs, agent_playbooks=[], profiles=prs
    )
    assert "[cs:s1] orphan rule" in md
    assert "[cs:p1] orphan pref" in md
    ids = {e["id"] for e in registry}
    assert ids == {"s1", "p1"}


def test_render_with_registry_fingerprint_disambiguates_same_rank() -> None:
    """Two renders with the same rank but different real ids → distinct tags."""
    md_a, _ = context_format.render_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "rule A", "user_playbook_id": 100}],
        agent_playbooks=[],
        profiles=[],
    )
    md_b, _ = context_format.render_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "rule B", "user_playbook_id": 200}],
        agent_playbooks=[],
        profiles=[],
    )
    assert "[cs:s1-100]" in md_a
    assert "[cs:s1-200]" in md_b


def test_render_with_registry_emits_citation_instruction() -> None:
    md, _ = context_format.render_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x"}],
        agent_playbooks=[],
        profiles=[],
    )
    assert cs_cite.CITATION_INSTRUCTION in md
    assert "When to cite:" in md
    assert "Do not call a shell command" not in md
    assert "✨ claude-smart rule applied: [verify process state]" in md
    assert "[brief answer preference]" in md
    assert "[⚡Reflexio](https://github.com/ReflexioAI/reflexio)" in md
    assert "Never emit a standalone wrapper" not in md
    assert "`✨ N claude-smart" not in md


def test_render_with_registry_uses_same_citation_instruction_for_codex() -> None:
    runtime.set_host(runtime.HOST_CODEX)

    md, _ = context_format.render_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x"}],
        agent_playbooks=[],
        profiles=[],
    )

    assert cs_cite.CITATION_INSTRUCTION in md


def test_render_with_registry_off_omits_citation_instruction(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CITATIONS", "off")
    md, _ = context_format.render_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x"}],
        agent_playbooks=[],
        profiles=[],
    )

    assert "marker line MUST be the very last line" not in md
    assert "claude-smart learning" not in md


def test_render_with_registry_playbook_trigger_and_rationale_emitted() -> None:
    pbs = [
        {
            "content": "use pathlib",
            "trigger": "writing a script",
            "rationale": "os.path is error-prone",
        }
    ]
    md, _ = context_format.render_with_registry(
        project_id="demo", user_playbooks=pbs, agent_playbooks=[], profiles=[]
    )
    assert "_(when: writing a script)_" in md
    assert "*why:* os.path is error-prone" in md


def test_render_with_registry_agent_playbooks_render_first_and_use_agent_id() -> None:
    """Agent playbooks (cross-project) are listed before user playbooks under
    one heading; the citation registry stamps them with the agent_playbook_id."""
    md, registry = context_format.render_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "user-scope rule", "user_playbook_id": 7}],
        agent_playbooks=[{"content": "global rule", "agent_playbook_id": 42}],
        profiles=[],
    )
    # Agent playbook bullet appears before the user one.
    agent_idx = md.find("global rule")
    user_idx = md.find("user-scope rule")
    assert 0 < agent_idx < user_idx
    # Both share the same playbook namespace; ranks are 1 then 2.
    assert "[cs:s1-42] global rule" in md
    assert "[cs:s2-7] user-scope rule" in md
    by_id = {e["id"]: e for e in registry}
    assert by_id["s1-42"]["real_id"] == "42"
    assert by_id["s1-42"]["source_kind"] == "agent_playbook"
    assert by_id["s2-7"]["real_id"] == "7"
    assert by_id["s2-7"]["source_kind"] == "user_playbook"


def test_render_inline_with_registry_uses_inline_headers() -> None:
    md, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "use pathlib"}],
        agent_playbooks=[],
        profiles=[{"content": "prefers anyio"}],
    )
    assert "### Relevant project-specific skills" in md
    assert "### Relevant project preferences" in md
    assert len(registry) == 2


def test_render_inline_with_registry_auto_mode_injects_compact_instruction(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CITATIONS", "auto")
    md, _ = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x"}],
        agent_playbooks=[],
        profiles=[],
    )
    assert cs_cite.CITATION_INSTRUCTION in md
    assert "citation block is up to two lines" not in md


def test_render_inline_with_registry_default_is_on(monkeypatch) -> None:
    """No env var set → compact citations are enabled."""
    monkeypatch.delenv("CLAUDE_SMART_CITATIONS", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_CITATION_LINK_STYLE", raising=False)
    md, _ = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x"}],
        agent_playbooks=[],
        profiles=[],
    )
    assert cs_cite.CITATION_INSTRUCTION in md


def test_render_inline_with_registry_can_inject_osc8_instruction(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CITATION_LINK_STYLE", "osc8")
    md, _ = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[
            {"content": "Use uv sync after pyproject edits.", "user_playbook_id": 17}
        ],
        agent_playbooks=[],
        profiles=[{"content": "prefers concise answers", "profile_id": "pref"}],
    )
    assert "OSC 8 terminal" in md
    assert "\x1b]8;;http://localhost:3001/rules/s1-123\x1b\\" in md
    assert "✨ claude-smart rule applied: [verify process state]" not in md
    assert "Include only memories that materially changed the response." in md
    assert "Available linked titles:" in md
    assert (
        "✨ claude-smart rule applied: "
        "\x1b]8;;http://localhost:3001/rules/s1-17\x1b\\"
        "Use uv sync after pyproject edits"
        "\x1b]8;;\x1b\\"
    ) in md
    assert (
        "Use uv sync after pyproject edits\x1b]8;;\x1b\\ | "
        "\x1b]8;;http://localhost:3001/rules/p1-pref"
    ) not in md
    # Reflexio attribution rides along as an OSC 8 terminal link to the repo.
    assert (
        " · \x1b]8;;https://github.com/ReflexioAI/reflexio\x1b\\⚡Reflexio\x1b]8;;\x1b\\"
    ) in md


def test_render_inline_compact_with_registry_is_one_logical_line(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_CITATION_LINK_STYLE", raising=False)
    md, registry = context_format.render_inline_compact_with_registry(
        project_id="demo",
        user_playbooks=[
            {
                "content": (
                    "Run uv sync after pyproject edits. "
                    "Then run an import smoke test before committing."
                ),
                "user_playbook_id": 17,
            }
        ],
        agent_playbooks=[],
        profiles=[{"content": "prefers concise answers", "profile_id": "pref"}],
    )

    assert md.endswith("\n")
    assert "\n" not in md.rstrip("\n")
    assert "###" not in md
    assert "- [cs:" not in md
    assert md.startswith("Relevant learnings from claude-smart")
    # No trigger: the label falls back to the first line of the content.
    assert (
        "Skill: [cs:s1-17] Run uv sync after pyproject edits. Then run an import "
        "smoke test before committing. (open: http://localhost:3001/rules/s1-17)."
    ) in md
    assert (
        "Preference: [cs:p1-pref] prefers concise answers "
        "(open: http://localhost:3001/rules/p1-pref)."
    ) in md
    assert "prefers concise answers" in md
    assert "✨ claude-smart rule applied:" in md
    assert md.count("✨ claude-smart rule applied:") == 1
    assert "Include only memories that materially changed the response." in md
    assert (
        "✨ claude-smart rule applied: "
        "[Run uv sync after pyproject edits](http://localhost:3001/rules/s1-17)"
    ) in md
    assert (
        "[Run uv sync after pyproject edits](http://localhost:3001/rules/s1-17) | "
        "[prefers concise answers](http://localhost:3001/rules/p1-pref)"
    ) not in md
    assert "visible ` | ` separator" in md
    assert "\x1b]8;;" not in md
    assert {entry["id"] for entry in registry} == {"s1-17", "p1-pref"}


def test_render_inline_compact_with_registry_can_emit_osc8_when_requested(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CITATION_LINK_STYLE", "osc8")
    md, registry = context_format.render_inline_compact_with_registry(
        project_id="demo",
        user_playbooks=[
            {
                "content": (
                    "Run uv sync after pyproject edits. "
                    "Then run an import smoke test before committing."
                ),
                "user_playbook_id": 17,
            }
        ],
        agent_playbooks=[],
        profiles=[{"content": "prefers concise answers", "profile_id": "pref"}],
    )

    assert "\x1b]8;;http://localhost:3001/rules/s1-17\x1b\\" in md
    assert "\x1b]8;;http://localhost:3001/rules/p1-pref\x1b\\" in md
    assert "Available linked titles:" not in md
    assert "Include only memories that materially changed the response." in md
    assert (
        "Run uv sync after pyproject edits\x1b]8;;\x1b\\ | "
        "\x1b]8;;http://localhost:3001/rules/p1-pref"
    ) not in md
    assert "open: http://localhost:3001/rules/s1-17" not in md
    assert {entry["id"] for entry in registry} == {"s1-17", "p1-pref"}


def test_render_inline_with_registry_marker_only_is_enabled_alias(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CITATIONS", "marker-only")
    md, _ = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x"}],
        agent_playbooks=[],
        profiles=[],
    )
    assert cs_cite.CITATION_INSTRUCTION in md
    assert "materially and meaningfully changed your response" in md
    assert "citation block is up to two lines" not in md


def test_render_inline_with_registry_includes_dashboard_urls(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_DASHBOARD_URL", "http://127.0.0.1:3333")
    md, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "use safe git flow", "user_playbook_id": 17}],
        agent_playbooks=[{"content": "use shared flow", "agent_playbook_id": 42}],
        profiles=[{"content": "prefers concise answers", "profile_id": "pref/one"}],
    )

    assert "open: http://127.0.0.1:3333/rules/s1-42" in md
    assert "open: http://127.0.0.1:3333/rules/s2-17" in md
    assert "open: http://127.0.0.1:3333/rules/p1-pref" in md
    by_id = {e["id"]: e for e in registry}
    assert by_id["s1-42"]["dashboard_url"] == "http://127.0.0.1:3333/skills/shared/42"
    assert by_id["s1-42"]["rule_url"] == "http://127.0.0.1:3333/rules/s1-42"
    assert by_id["s2-17"]["dashboard_url"] == "http://127.0.0.1:3333/skills/project/17"
    assert by_id["s2-17"]["rule_url"] == "http://127.0.0.1:3333/rules/s2-17"
    assert (
        by_id["p1-pref"]["dashboard_url"]
        == "http://127.0.0.1:3333/preferences/project/pref%2Fone"
    )
    assert by_id["p1-pref"]["rule_url"] == "http://127.0.0.1:3333/rules/p1-pref"


def test_render_inline_with_registry_uses_remote_reflexio_item_pages(
    monkeypatch,
) -> None:
    monkeypatch.setenv("REFLEXIO_URL", "https://www.reflexio.ai/")
    md, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "use safe git flow", "user_playbook_id": 17}],
        agent_playbooks=[{"content": "use shared flow", "agent_playbook_id": 42}],
        profiles=[{"content": "prefers concise answers", "profile_id": "pref/one"}],
    )

    assert "open: https://www.reflexio.ai/playbooks?agent_playbook_id=42" in md
    assert (
        "open: https://www.reflexio.ai/playbooks?"
        "resource=user_playbook&user_playbook_id=17"
    ) in md
    assert "open: https://www.reflexio.ai/profiles?profile_id=pref%2Fone" in md
    assert "/rules/" not in md
    by_id = {e["id"]: e for e in registry}
    assert (
        by_id["s1-42"]["dashboard_url"]
        == "https://www.reflexio.ai/playbooks?agent_playbook_id=42"
    )
    assert (
        by_id["s1-42"]["rule_url"]
        == "https://www.reflexio.ai/playbooks?agent_playbook_id=42"
    )
    assert (
        by_id["s2-17"]["dashboard_url"]
        == "https://www.reflexio.ai/playbooks?resource=user_playbook&user_playbook_id=17"
    )
    assert (
        by_id["s2-17"]["rule_url"]
        == "https://www.reflexio.ai/playbooks?resource=user_playbook&user_playbook_id=17"
    )
    assert (
        by_id["p1-pref"]["dashboard_url"]
        == "https://www.reflexio.ai/profiles?profile_id=pref%2Fone"
    )
    assert (
        by_id["p1-pref"]["rule_url"]
        == "https://www.reflexio.ai/profiles?profile_id=pref%2Fone"
    )


def test_render_inline_compact_uses_remote_reflexio_item_pages(monkeypatch) -> None:
    monkeypatch.setenv("REFLEXIO_URL", "https://www.reflexio.ai/")

    md, _ = context_format.render_inline_compact_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "use safe git flow", "user_playbook_id": 17}],
        agent_playbooks=[{"content": "use shared flow", "agent_playbook_id": 42}],
        profiles=[{"content": "prefers concise answers", "profile_id": "pref/one"}],
    )

    assert (
        "[use shared flow](https://www.reflexio.ai/playbooks?agent_playbook_id=42)"
    ) in md
    assert (
        "open: https://www.reflexio.ai/playbooks?"
        "resource=user_playbook&user_playbook_id=17"
    ) in md
    assert "open: https://www.reflexio.ai/profiles?profile_id=pref%2Fone" in md
    assert "http://localhost:3001/rules/" not in md


def test_render_inline_osc8_uses_remote_reflexio_item_pages(monkeypatch) -> None:
    monkeypatch.setenv("REFLEXIO_URL", "https://www.reflexio.ai/")
    monkeypatch.setenv("CLAUDE_SMART_CITATION_LINK_STYLE", "osc8")

    md, _ = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "use safe git flow", "user_playbook_id": 17}],
        agent_playbooks=[],
        profiles=[{"content": "prefers concise answers", "profile_id": "pref/one"}],
    )

    assert (
        "\x1b]8;;https://www.reflexio.ai/playbooks?"
        "resource=user_playbook&user_playbook_id=17\x1b\\"
    ) in md
    assert "\x1b]8;;https://www.reflexio.ai/profiles?profile_id=pref%2Fone\x1b\\" in md
    assert "http://localhost:3001/rules/s1-17" not in md
    assert "http://localhost:3001/rules/p1-pref" not in md


def test_render_inline_with_registry_remote_items_without_real_ids_use_list_pages(
    monkeypatch,
) -> None:
    monkeypatch.setenv("REFLEXIO_URL", "https://www.reflexio.ai/")
    md, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "use safe git flow"}],
        agent_playbooks=[],
        profiles=[{"content": "prefers concise answers"}],
    )

    assert "open: https://www.reflexio.ai/playbooks" in md
    assert "open: https://www.reflexio.ai/profiles" in md
    by_id = {e["id"]: e for e in registry}
    assert by_id["s1"]["rule_url"] == "https://www.reflexio.ai/playbooks"
    assert by_id["p1"]["rule_url"] == "https://www.reflexio.ai/profiles"


def test_render_inline_with_registry_off_omits_instruction(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CITATIONS", "off")
    md, _ = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x"}],
        agent_playbooks=[],
        profiles=[],
    )
    assert "marker line MUST be the very last line" not in md
    assert "claude-smart learning" not in md


def test_title_from_content_short_content_kept_intact() -> None:
    assert context_format._title_from_content("short content") == "short content"


def test_title_from_content_truncates_with_ellipsis() -> None:
    long = "a" * 200
    out = context_format._title_from_content(long, limit=10)
    assert out.endswith("…")
    assert len(out) == 10


def test_title_from_content_splits_on_sentence_boundary() -> None:
    text = "First sentence. Second sentence."
    assert context_format._title_from_content(text) == "First sentence"


@pytest.mark.parametrize("link_style", ["markdown", "osc8"])
def test_compact_citation_guidance_does_not_grow_with_unused_memories(
    monkeypatch,
    link_style: str,
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CITATION_LINK_STYLE", link_style)
    titles = [f"rule-{index}" for index in range(50)]
    assert context_format._compact_citation_instruction(titles) == (
        context_format._compact_citation_instruction(titles[:1])
    )


def _index_hits(count: int, prefix: str) -> list[dict]:
    return [
        {
            f"{prefix}_playbook_id": f"{prefix}{index}",
            "content": f"FULL {prefix} {index} content. " + "detail " * 400,
            "trigger": f"when {prefix} {index} applies " + "word " * 60,
            "rationale": "because " + "y" * 300,
        }
        for index in range(1, count + 1)
    ]


def _index_profiles(count: int) -> list[dict]:
    return [
        {"profile_id": f"pref{index}", "content": f"pref {index} " + "z " * 400}
        for index in range(1, count + 1)
    ]


def test_index_label_uses_trigger_and_never_content() -> None:
    entry = {"trigger": "editing pyproject.toml", "content": "SECRET body"}
    assert context_format.index_label(entry) == "editing pyproject.toml"


def test_index_label_cuts_at_word_boundary() -> None:
    trigger = " ".join(f"word{index}" for index in range(60))
    label = context_format.index_label({"trigger": trigger, "content": "x"})
    assert label.endswith("…")
    assert len(label) <= context_format.INDEX_LABEL_MAX_CHARS + 1
    assert trigger.startswith(label[:-1])
    # The cut lands on a whole word: the next character in the source is a space.
    assert trigger[len(label) - 1] == " "


def test_index_label_never_splits_a_word_at_the_limit() -> None:
    trigger = "x" * 145 + " lengthyword more words"
    label = context_format.index_label({"trigger": trigger, "content": "x"})
    assert label == "x" * 145 + "…"


def test_marker_title_comes_from_index_label_and_keeps_whole_words(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SMART_CITATION_LINK_STYLE", raising=False)
    md, _ = context_format.render_inline_compact_with_registry(
        project_id="demo",
        user_playbooks=[
            {
                "user_playbook_id": 403,
                "content": "SECRET body text.",
                "trigger": "checking out a branch for a worktree that another agent "
                "worktree may already hold",
            }
        ],
        agent_playbooks=[],
        profiles=[],
    )
    assert (
        "[checking out a branch for a worktree that another agent]"
        "(http://localhost:3001/rules/s1-403)"
    ) in md
    assert "SECRET" not in md


def test_index_label_hard_cuts_a_single_long_word() -> None:
    label = context_format.index_label({"trigger": "a" * 400, "content": "x"})
    assert label == "a" * context_format.INDEX_LABEL_MAX_CHARS + "…"


def test_index_label_falls_back_to_first_content_line_without_trigger() -> None:
    entry = {"trigger": "", "content": "Use uv sync.\nSecond line stays hidden."}
    assert context_format.index_label(entry) == "Use uv sync."


def test_index_label_for_profile_uses_cut_content() -> None:
    entry = {"kind": "profile", "content": "prefers " + "very " * 100 + "short"}
    label = context_format.index_label(entry)
    assert label.startswith("prefers very")
    assert label.endswith("…")
    assert len(label) <= context_format.INDEX_LABEL_MAX_CHARS + 1


def test_render_inline_index_lines_show_trigger_link_and_loader(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_SMART_CITATION_LINK_STYLE", raising=False)
    md, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[
            {
                "user_playbook_id": 17,
                "content": "Run uv sync after pyproject edits.",
                "trigger": "editing pyproject.toml",
                "rationale": "stale lockfile broke CI",
            }
        ],
        agent_playbooks=[],
        profiles=[{"profile_id": "pref", "content": "prefers concise answers"}],
        session_id="sess-1",
    )
    lines = md.splitlines()
    assert lines[0] == (
        "Relevant learnings from claude-smart — each line says when one applies. "
        "Before relying on one that applies, load its full text with: "
        f"`{context_format.LOADER_COMMAND} --session sess-1 <id>`"
    )
    assert (
        "- [cs:s1-17] editing pyproject.toml "
        "_(open: http://localhost:3001/rules/s1-17)_"
    ) in lines
    assert (
        "- [cs:p1-pref] prefers concise answers "
        "_(open: http://localhost:3001/rules/p1-pref)_"
    ) in lines
    assert "Run uv sync after pyproject edits" not in md
    assert "stale lockfile" not in md
    # The registry caches the full item for the loader.
    assert registry[0]["content"] == "Run uv sync after pyproject edits."
    assert registry[0]["trigger"] == "editing pyproject.toml"
    assert registry[0]["rationale"] == "stale lockfile broke CI"


def test_render_inline_omits_session_flag_without_session() -> None:
    md, _ = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[{"content": "x", "trigger": "y"}],
        agent_playbooks=[],
        profiles=[],
    )
    assert f"`{context_format.LOADER_COMMAND} <id>`" in md
    assert "--session" not in md


@pytest.mark.parametrize("link_style", ["markdown", "osc8"])
@pytest.mark.parametrize(
    ("renderer", "budget"),
    [
        (
            context_format.render_inline_with_registry,
            context_format.INLINE_CONTEXT_BUDGET_CHARS,
        ),
        (
            context_format.render_inline_compact_with_registry,
            context_format.CODEX_INLINE_CONTEXT_BUDGET_CHARS,
        ),
    ],
)
def test_render_inline_fits_fifteen_items_within_budget(
    monkeypatch, renderer, budget: int, link_style: str
) -> None:
    """Five hits per collection, all with maximal labels, fit the budget."""
    monkeypatch.setenv("CLAUDE_SMART_CITATION_LINK_STYLE", link_style)
    md, registry = renderer(
        project_id="demo",
        user_playbooks=_index_hits(5, "user"),
        agent_playbooks=_index_hits(5, "agent"),
        profiles=_index_profiles(5),
        session_id="0b6f1c1e-6a8f-4d3b-9b8e-3d1f2a7c9e10",
    )
    assert len(md) <= budget
    assert len(registry) == 15
    assert "FULL" not in md
    assert "because" not in md
    for entry in registry:
        assert f"[cs:{entry['id']}]" in md


def test_render_inline_registry_matches_shown_index_lines() -> None:
    md, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=_index_hits(5, "user"),
        agent_playbooks=_index_hits(5, "agent"),
        profiles=_index_profiles(5),
    )
    shown_ids = re.findall(r"^- \[cs:([^\]]+)\]", md, flags=re.MULTILINE)
    assert sorted(shown_ids) == sorted(entry["id"] for entry in registry)


@pytest.mark.parametrize(
    "renderer",
    [
        context_format.render_inline_with_registry,
        context_format.render_inline_compact_with_registry,
    ],
)
def test_render_inline_overflow_drops_only_lowest_ranked_lines(renderer) -> None:
    """The budget backstop keeps a rank-order prefix (s1, p1, s2, p2, ...)."""
    kwargs = {
        "project_id": "demo",
        "user_playbooks": [],
        "agent_playbooks": _index_hits(5, "agent"),
        "profiles": _index_profiles(5),
    }
    full_md, full_registry = renderer(**kwargs, budget_chars=100_000)
    assert len(full_registry) == 10
    md, registry = renderer(**kwargs, budget_chars=len(full_md) - 1)
    ids = [entry["id"] for entry in registry]
    assert len(md) < len(full_md)
    # Order is s1, p1, s2, p2, ..., s5, p5, so one char short drops p5 only.
    assert ids == [f"s{rank}-agen" for rank in range(1, 6)] + [
        f"p{rank}-pref" for rank in range(1, 5)
    ]
    assert "p5-pref" not in ids
    assert "[cs:p5-pref]" not in md


@pytest.mark.parametrize(
    "renderer",
    [
        context_format.render_inline_with_registry,
        context_format.render_inline_compact_with_registry,
    ],
)
def test_render_inline_never_emits_header_or_footer_alone(renderer) -> None:
    md, registry = renderer(
        project_id="demo",
        user_playbooks=_index_hits(1, "user"),
        agent_playbooks=[],
        profiles=[],
        budget_chars=200,
    )
    assert (md, registry) == ("", [])


def test_inline_budget_chars_per_host() -> None:
    assert context_format.inline_budget_chars("claude-code") == 8_000
    assert context_format.inline_budget_chars("opencode") == 8_000
    assert context_format.inline_budget_chars("codex") == 6_000
    assert context_format.CODEX_INLINE_CONTEXT_BUDGET_CHARS < (
        context_format.INLINE_CONTEXT_BUDGET_CHARS
    )
    assert context_format.INLINE_CONTEXT_BUDGET_CHARS < 10_000


def test_render_loaded_item_prints_full_text() -> None:
    _, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[
            {
                "user_playbook_id": 17,
                "content": "Run uv sync after pyproject edits.",
                "trigger": "editing pyproject.toml",
                "rationale": "stale lockfile broke CI",
            }
        ],
        agent_playbooks=[],
        profiles=[],
    )
    assert context_format.render_loaded_item(registry[0]) == (
        "[cs:s1-17] project skill\n"
        "When: editing pyproject.toml\n"
        "Run uv sync after pyproject edits.\n"
        "Why: stale lockfile broke CI\n"
        "Open: http://localhost:3001/rules/s1-17\n"
    )
