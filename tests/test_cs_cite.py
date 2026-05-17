"""Tests for claude-smart citation support helpers."""

from __future__ import annotations

import pytest

from claude_smart import cs_cite


def test_parse_text_citations_accepts_codex_learning_marker() -> None:
    text = (
        "The real answer remains visible.\n\n"
        "✨ 2 claude-smart learnings applied [cs:s2-cd34,p1-ab12]"
    )
    assert cs_cite.parse_text_citations(text) == ["s2-cd34", "p1-ab12"]


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
