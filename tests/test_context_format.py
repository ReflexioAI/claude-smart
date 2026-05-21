"""Tests for the context-markdown renderer and citation registry."""

from __future__ import annotations

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
    assert "If you use any listed" in md
    assert "Do not call a shell command" not in md
    assert "✨ claude-smart rule applied: [verify process state]" in md
    assert "[brief answer preference]" in md
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
    assert "copy this exact final marker" in md
    assert "Do not rename, summarize, or regroup the linked titles." in md
    assert (
        "✨ claude-smart rule applied: "
        "\x1b]8;;http://localhost:3001/rules/s1-17\x1b\\"
        "Use uv sync after pyproject edits"
        "\x1b]8;;\x1b\\ | "
        "\x1b]8;;http://localhost:3001/rules/p1-pref\x1b\\"
        "prefers concise answers"
        "\x1b]8;;\x1b\\"
    ) in md


def test_render_inline_compact_with_registry_is_one_logical_line(
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

    assert md.endswith("\n")
    assert "\n" not in md.rstrip("\n")
    assert "###" not in md
    assert "- [cs:" not in md
    assert "[cs:" not in md
    assert "claude-smart: using relevant memory. Skill:" in md
    assert "Preference:" in md
    assert "\x1b]8;;http://localhost:3001/rules/s1-17\x1b\\" in md
    assert "Run uv sync after pyproject edits" in md
    assert "Then run an import smoke test before committing" in md
    assert "Run uv sync after pyproject edits: Run uv sync" not in md
    assert "title:" not in md
    assert "\x1b]8;;http://localhost:3001/rules/p1-pref\x1b\\" in md
    assert "prefers concise answers" in md
    assert "✨ claude-smart rule applied:" in md
    assert md.count("✨ claude-smart rule applied:") == 1
    assert "preserving its hidden OSC 8 terminal link" in md
    assert (
        "✨ claude-smart rule applied: "
        "\x1b]8;;http://localhost:3001/rules/s1-17\x1b\\"
        "Run uv sync after pyproject edits"
        "\x1b]8;;\x1b\\ | "
        "\x1b]8;;http://localhost:3001/rules/p1-pref\x1b\\"
        "prefers concise answers"
        "\x1b]8;;\x1b\\"
    ) in md
    assert "visible ` | ` separator" in md
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
    assert "materially changes your answer" in md
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
