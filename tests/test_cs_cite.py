"""Tests for the ``cs-cite`` support helpers and installer."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from claude_smart import cs_cite


def test_parse_citation_command_accepts_bare_invocation() -> None:
    assert cs_cite.parse_citation_command("cs-cite p1-ab12,s2-cd34") == [
        "p1-ab12",
        "s2-cd34",
    ]


def test_parse_citation_command_accepts_single_id() -> None:
    assert cs_cite.parse_citation_command("cs-cite p1-ab12") == ["p1-ab12"]


def test_parse_citation_command_accepts_id_without_fingerprint() -> None:
    """Bare ``p1`` / ``s1`` still parse — fingerprint is optional."""
    assert cs_cite.parse_citation_command("cs-cite p1") == ["p1"]
    assert cs_cite.parse_citation_command("cs-cite p1,s2") == ["p1", "s2"]


def test_parse_citation_command_accepts_absolute_path_prefix() -> None:
    """The regex documents an optional path prefix for bare TUI rendering."""
    cmd = "/Users/me/.claude-smart/bin/cs-cite s3-abcd"
    assert cs_cite.parse_citation_command(cmd) == ["s3-abcd"]


def test_parse_citation_command_rejects_chained_commands() -> None:
    assert cs_cite.parse_citation_command("cs-cite p1-ab12 && echo ok") == []
    assert cs_cite.parse_citation_command("cs-cite p1-ab12 | cat") == []
    assert cs_cite.parse_citation_command("cs-cite p1-ab12; echo ok") == []


def test_parse_citation_command_rejects_malformed_ids() -> None:
    assert cs_cite.parse_citation_command("cs-cite xxxx") == []
    assert cs_cite.parse_citation_command("cs-cite ab12") == []
    assert cs_cite.parse_citation_command("cs-cite p") == []
    assert cs_cite.parse_citation_command("cs-cite q1-abcd") == []
    # Fingerprint > 4 chars is rejected.
    assert cs_cite.parse_citation_command("cs-cite p1-abcde") == []
    # Empty fingerprint after dash is rejected.
    assert cs_cite.parse_citation_command("cs-cite p1-") == []


def test_parse_citation_command_normalizes_uppercase() -> None:
    """Uppercase ids (prefix, kind, fingerprint) are normalized to lowercase."""
    assert cs_cite.parse_citation_command("cs-cite P1-AB12") == ["p1-ab12"]
    assert cs_cite.parse_citation_command("cs-cite P1-AB12,S2-CD34") == [
        "p1-ab12",
        "s2-cd34",
    ]


def test_parse_citation_command_strips_cs_prefix() -> None:
    """The `cs:` prefix from `[cs:xxxx]` tags is stripped automatically."""
    assert cs_cite.parse_citation_command("cs-cite cs:p1-ab12") == ["p1-ab12"]
    assert cs_cite.parse_citation_command("cs-cite cs:p1-ab12,cs:s2-cd34") == [
        "p1-ab12",
        "s2-cd34",
    ]
    assert cs_cite.parse_citation_command("cs-cite cs:p1-ab12,s2-cd34") == [
        "p1-ab12",
        "s2-cd34",
    ]


def test_parse_citation_command_accepts_uppercase_cs_prefix() -> None:
    """An uppercase `CS:` prefix is tolerated — the bin script accepts it too."""
    assert cs_cite.parse_citation_command("cs-cite CS:p1-ab12") == ["p1-ab12"]
    assert cs_cite.parse_citation_command("cs-cite CS:P1-AB12,Cs:S2-CD34") == [
        "p1-ab12",
        "s2-cd34",
    ]


def test_parse_citation_command_accepts_whitespace_separators() -> None:
    """Whitespace between ids is accepted alongside commas."""
    assert cs_cite.parse_citation_command("cs-cite p1-ab12 s2-cd34") == [
        "p1-ab12",
        "s2-cd34",
    ]
    assert cs_cite.parse_citation_command("cs-cite p1-ab12, s2-cd34") == [
        "p1-ab12",
        "s2-cd34",
    ]


def test_parse_citation_command_accepts_multi_digit_ranks() -> None:
    """Rank numbers may grow beyond single digits."""
    assert cs_cite.parse_citation_command("cs-cite p10-abcd,s42-ef01") == [
        "p10-abcd",
        "s42-ef01",
    ]


def test_parse_citation_command_rejects_embedded_path_traversal() -> None:
    """A path prefix is allowed, but must not contain spaces or extra tokens."""
    assert cs_cite.parse_citation_command("/tmp/../evil cs-cite p1-ab12") == []


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
    """Same rank + different real ids → distinct ids (core collision fix)."""
    a = cs_cite.rank_id("playbook", 1, 100)
    b = cs_cite.rank_id("playbook", 1, 200)
    assert a != b
    assert a == "s1-100"
    assert b == "s1-200"


def test_rank_id_real_id_without_alphanumeric_falls_back_to_rank() -> None:
    """An id like ``"---"`` has no alphanumeric prefix → suffix omitted."""
    assert cs_cite.rank_id("profile", 1, "---") == "p1"


def test_rank_id_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        cs_cite.rank_id("other", 1)


def test_ensure_installed_is_idempotent_and_executable(tmp_path, monkeypatch) -> None:
    """Two calls produce an executable target; the second call is a no-op."""
    monkeypatch.setattr(cs_cite, "_INSTALL_DIR", tmp_path / "bin")
    monkeypatch.setattr(cs_cite, "INSTALL_PATH", tmp_path / "bin" / "cs-cite")
    cs_cite.ensure_installed()
    target = tmp_path / "bin" / "cs-cite"
    assert target.is_file()
    mode = target.stat().st_mode
    assert mode & stat.S_IXUSR
    first_mtime = target.stat().st_mtime_ns
    # Second call must not raise and must leave the bit set.
    cs_cite.ensure_installed()
    assert target.stat().st_mode & stat.S_IXUSR
    # copy2 preserves the source mtime, so the file's mtime should be stable
    # across repeated calls (guards against a re-download/re-build loop).
    assert target.stat().st_mtime_ns == first_mtime


def test_ensure_installed_tolerates_readonly_target_parent(
    tmp_path, monkeypatch
) -> None:
    """Install is best-effort: read-only parent dir must not raise."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses the read-only bit")
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        monkeypatch.setattr(cs_cite, "_INSTALL_DIR", parent / "bin")
        monkeypatch.setattr(cs_cite, "INSTALL_PATH", parent / "bin" / "cs-cite")
        # Must not raise.
        cs_cite.ensure_installed()
    finally:
        parent.chmod(0o700)


def _run_cs_cite(*argv: str) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parents[1] / "plugin" / "bin" / "cs-cite"
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cs_cite_script_is_silent_on_success() -> None:
    """The script no longer prints the confirmation line; the model does.

    This gives Claude a natural trailing thing to emit after the Bash call,
    which fixes a role-marker hallucination bug caused by framing cs-cite
    as a turn-terminator when it is actually a regular tool call.
    """
    r = _run_cs_cite("p1-ab12")
    assert r.returncode == 0
    assert r.stdout == ""


def test_cs_cite_script_silent_for_multiple_ids() -> None:
    r = _run_cs_cite("p1-ab12,s2-cd34,p3-ef56")
    assert r.returncode == 0
    assert r.stdout == ""


def test_cs_cite_script_rejects_no_args() -> None:
    r = _run_cs_cite()
    assert r.returncode == 1
    assert "rank ids" in r.stderr


def test_cs_cite_script_accepts_space_separated_argv() -> None:
    """Multiple argv tokens are joined; Stop-side regex tolerates the shape."""
    r = _run_cs_cite("p1-ab12", "s2-cd34")
    assert r.returncode == 0
    assert r.stdout == ""


def test_cs_cite_script_strips_cs_prefix() -> None:
    """Ids copied verbatim from `[cs:xxxx]` tags are accepted."""
    r = _run_cs_cite("cs:p1-ab12,cs:s2-cd34,cs:p3-ef56")
    assert r.returncode == 0
    assert r.stdout == ""


def test_cs_cite_script_normalizes_uppercase() -> None:
    r = _run_cs_cite("P1-AB12,S2-CD34")
    assert r.returncode == 0
    assert r.stdout == ""


def test_cs_cite_script_accepts_bare_rank_without_fingerprint() -> None:
    """Fingerprint-less ids remain valid for back-compat with registry entries
    that had no real id."""
    r = _run_cs_cite("p1,s2")
    assert r.returncode == 0
    assert r.stdout == ""


def test_cs_cite_script_rejects_malformed_ids() -> None:
    r = _run_cs_cite("xxxx")
    assert r.returncode == 1
    assert "rank ids" in r.stderr


def test_cs_cite_script_warns_on_mixed_valid_and_invalid() -> None:
    """Valid ids still succeed (silently); invalid tokens are flagged on stderr."""
    r = _run_cs_cite("p1-ab12,notarank,s3-cd34")
    assert r.returncode == 0
    assert r.stdout == ""
    assert "notarank" in r.stderr
