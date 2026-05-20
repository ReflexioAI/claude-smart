"""Tests for claude-smart citation support helpers."""

from __future__ import annotations

import re

import pytest
from claude_smart import cs_cite


def test_parse_text_citations_accepts_codex_learning_marker() -> None:
    text = (
        "The real answer remains visible.\n\n"
        "✨ 2 claude-smart learnings applied [cs:s2-cd34,p1-ab12]"
    )
    assert cs_cite.parse_text_citations(text) == ["s2-cd34", "p1-ab12"]


def test_parse_text_citations_accepts_human_dashboard_marker() -> None:
    text = (
        "The real answer remains visible.\n\n"
        "✨ claude-smart rules applied: "
        "[git safety](http://localhost:3001/rules/s1-7536), "
        "[brief answer preference](http://localhost:3001/rules/p1-code)"
    )
    assert cs_cite.parse_text_citations(text) == [
        "s1-7536",
        "p1-code",
    ]


def test_parse_text_citations_accepts_shared_skill_dashboard_marker() -> None:
    text = (
        "Done.\n\n"
        "✨ claude-smart rule applied: "
        "[shared rule](http://localhost:3001/skills/shared/42)"
    )
    assert cs_cite.parse_text_citations(text) == ["route:playbook:agent_playbook:42"]


def test_parse_text_citations_accepts_raw_dashboard_urls() -> None:
    text = (
        "Done.\n\n"
        "✨ claude-smart rule applied: "
        "git safety http://localhost:3001/skills/project/7536"
    )
    assert cs_cite.parse_text_citations(text) == ["route:playbook:user_playbook:7536"]


def test_parse_text_citations_accepts_osc8_dashboard_links() -> None:
    text = (
        "Done.\n\n"
        "✨ claude-smart rules applied: "
        "\x1b]8;;http://localhost:3001/rules/s1-7536\x1b\\git safety\x1b]8;;\x1b\\, "
        "\x1b]8;;http://localhost:3001/rules/p1-pref\x1b\\brief answers\x1b]8;;\x1b\\"
    )
    assert cs_cite.parse_text_citations(text) == [
        "s1-7536",
        "p1-pref",
    ]


def test_parse_text_citations_accepts_direct_dashboard_links() -> None:
    text = (
        "Done.\n\n"
        "✨ claude-smart rules applied: "
        "[git safety](http://localhost:3001/skills/project/7536), "
        "[brief answers](http://localhost:3001/preferences/project/pref-1)"
    )
    assert cs_cite.parse_text_citations(text) == [
        "route:playbook:user_playbook:7536",
        "route:profile:profile:pref-1",
    ]


def test_parse_text_citations_keeps_old_applied_marker_compatible() -> None:
    text = "Done.\n\n✨ Applied: [git safety](http://localhost:3001/skills/project/7536)"
    assert cs_cite.parse_text_citations(text) == ["route:playbook:user_playbook:7536"]


def test_parse_text_citations_keeps_legacy_preference_route_compatible() -> None:
    text = (
        "Done.\n\n"
        "✨ claude-smart rule applied: "
        "[brief answers](http://localhost:3001/preferences/pref-1)"
    )
    assert cs_cite.parse_text_citations(text) == ["route:profile:profile:pref-1"]


def test_parse_text_citations_ignores_plain_inline_tags() -> None:
    text = "This mentions [cs:s2-cd34] but has no learning marker."
    assert cs_cite.parse_text_citations(text) == []


def test_parse_text_citations_rejects_standalone_wrapper() -> None:
    assert cs_cite.parse_text_citations("Answer.\n\n✨s2-cd34✨") == []
    assert (
        cs_cite.parse_text_citations("Answer.\n\n✨1gkgg8b9r7fx99kr2j3q6k5c1v✨")
        == []
    )


def test_parse_text_citations_uses_last_marker_line() -> None:
    text = (
        "✨ 1 claude-smart learning applied [cs:s1-1111]\n"
        "answer\n"
        "✨ 1 claude-smart learning applied [cs:p2-2222]"
    )
    assert cs_cite.parse_text_citations(text) == ["p2-2222"]


def test_parse_text_citations_accepts_uppercase_and_cs_prefixes() -> None:
    text = "Done.\n\n✨ 2 claude-smart learnings applied [cs:CS:P1-AB12,Cs:S2-CD34]"
    assert cs_cite.parse_text_citations(text) == ["p1-ab12", "s2-cd34"]


def test_parse_text_citations_accepts_whitespace_separators() -> None:
    text = "Done.\n\n✨ 2 claude-smart learnings applied [cs:p1-ab12 s2-cd34]"
    assert cs_cite.parse_text_citations(text) == ["p1-ab12", "s2-cd34"]


def test_parse_text_citations_rejects_malformed_ids() -> None:
    text = "Done.\n\n✨ 3 claude-smart learnings applied [cs:p1-ab12,xxxx,s2-cd34]"
    assert cs_cite.parse_text_citations(text) == ["p1-ab12", "s2-cd34"]


def test_rank_id_without_real_id_omits_fingerprint() -> None:
    assert cs_cite.rank_id("profile", 1) == "p1"
    assert cs_cite.rank_id("profile", 7) == "p7"
    assert cs_cite.rank_id("playbook", 1) == "s1"
    assert cs_cite.rank_id("playbook", 12) == "s12"


def test_rank_id_appends_fingerprint_from_real_id() -> None:
    """Fingerprint is the first 4 alphanumeric chars of ``str(real_id)``, lowercased."""
    assert cs_cite.rank_id("profile", 1, 17) == "p1-17"
    assert cs_cite.rank_id("playbook", 2, "uuid-profile-1") == "s2-uuid"
    assert cs_cite.rank_id("playbook", 3, "AbCdEfGh") == "s3-abcd"


def test_rank_id_disambiguates_across_injections() -> None:
    """Same rank + different real ids -> distinct ids."""
    a = cs_cite.rank_id("playbook", 1, 100)
    b = cs_cite.rank_id("playbook", 1, 200)
    assert a != b
    assert a == "s1-100"
    assert b == "s1-200"


def test_rank_id_real_id_without_alphanumeric_falls_back_to_rank() -> None:
    """An id like ``"---"`` has no alphanumeric prefix -> suffix omitted."""
    assert cs_cite.rank_id("profile", 1, "---") == "p1"


def test_rank_id_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        cs_cite.rank_id("other", 1)


def test_citation_instruction_auto_returns_full_string() -> None:
    text = cs_cite.citation_instruction("auto")
    assert text == cs_cite.CITATION_INSTRUCTION
    assert "citation block is up to two lines" in text
    assert "marker line MUST be the very last line" in text
    assert "✨ claude-smart rule applied:" in text


def test_citation_instruction_marker_only_drops_counterfactual_paragraph() -> None:
    text = cs_cite.citation_instruction("marker-only")
    assert "citation block is up to two lines" not in text
    assert "counterfactual" not in text.lower()
    assert "marker line MUST be the very last line" in text
    assert "✨ claude-smart rule applied:" in text


def test_citation_instruction_osc8_uses_terminal_hyperlink_examples() -> None:
    text = cs_cite.citation_instruction("marker-only", "osc8")
    assert "OSC 8 terminal" in text
    assert "\x1b]8;;http://localhost:3001/rules/s1-123\x1b\\" in text
    assert "[git safety](http://localhost:3001/rules/s1-123)" in text


def test_citation_instruction_off_returns_empty_string() -> None:
    assert cs_cite.citation_instruction("off") == ""


def test_citation_instruction_unknown_falls_back_to_auto() -> None:
    """Env-var typos must not break injection — unknown modes behave like ``auto``."""
    assert cs_cite.citation_instruction("typo") == cs_cite.citation_instruction("auto")


def test_citation_instruction_auto_counterfactual_does_not_reference_rank_id() -> None:
    """Regression: the counterfactual example must not show a bare ``s\\d+-`` /
    ``p\\d+-`` token, or the assistant copies the cryptic id pattern."""
    text = cs_cite.citation_instruction("auto")
    # The marker paragraph starts here; rank-id examples are allowed in it
    # but must not appear in the preceding counterfactual paragraph.
    marker_para_start = text.index("End the message with exactly one marker line")
    counterfactual_para_start = text.index("citation block is up to two lines")
    counterfactual_para = text[counterfactual_para_start:marker_para_start]
    assert not re.search(r"\b[sp]\d+-[a-z0-9]+\b", counterfactual_para)


def test_strip_marker_lines_removes_single_marker() -> None:
    text = (
        "Here's the answer.\n\n"
        "✨ 1 claude-smart learning applied [cs:s1-1a2b]"
    )
    assert cs_cite.strip_marker_lines(text) == "Here's the answer."


def test_strip_marker_lines_removes_human_dashboard_marker() -> None:
    text = (
        "Here's the answer.\n\n"
        "✨ claude-smart rule applied: "
        "[git safety](http://localhost:3001/skills/project/7536)"
    )
    assert cs_cite.strip_marker_lines(text) == "Here's the answer."


def test_strip_marker_lines_removes_multiple_markers_inline() -> None:
    text = (
        "intro\n"
        "✨ 1 claude-smart learning applied [cs:s1-1a2b]\n"
        "middle\n"
        "✨ 2 claude-smart learnings applied [cs:s1-1a2b,p2-cd34]\n"
    )
    out = cs_cite.strip_marker_lines(text)
    assert "✨" not in out
    assert "intro" in out
    assert "middle" in out


def test_strip_marker_lines_leaves_unrelated_text_alone() -> None:
    text = "This mentions ✨ but is not a marker."
    assert cs_cite.strip_marker_lines(text) == text


def test_strip_marker_lines_handles_empty_input() -> None:
    assert cs_cite.strip_marker_lines("") == ""
    assert cs_cite.strip_marker_lines(None) == ""  # type: ignore[arg-type]
