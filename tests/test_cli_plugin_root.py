"""~/.reflexio/plugin-root bookkeeping, session lookup and service scripts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from claude_smart import cli


def test_latest_session_id_picks_newest_buffer(session_dir: Path) -> None:
    assert cli._latest_session_id() is None
    old = session_dir / "older.jsonl"
    new = session_dir / "newer.jsonl"
    old.write_text("")
    new.write_text("")
    os.utime(old, (100, 100))
    os.utime(new, (200, 200))
    (session_dir / "ignored.txt").write_text("")

    assert cli._latest_session_id() == "newer"


def test_latest_session_id_when_state_dir_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(tmp_path / "missing"))
    assert cli._latest_session_id() is None


# --- executable resolution ---------------------------------------------------


def test_resolve_candidate_expands_vars_and_falls_back_to_path(
    monkeypatch, tmp_path
) -> None:
    tool = tmp_path / "bin" / "tool"
    tool.parent.mkdir()
    tool.write_text("")
    tool.chmod(0o755)
    monkeypatch.setenv("TOOL_HOME", str(tmp_path / "bin"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert cli._resolve_candidate("$TOOL_HOME/tool") == str(tool)
    assert cli._resolve_candidate("tool") == str(tool)
    assert cli._resolve_candidate("no-such-tool") is None


def test_iter_path_candidates_skips_empty_entries(monkeypatch, tmp_path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    for directory in (first, second):
        directory.mkdir()
    (second / "bash").write_text("")
    monkeypatch.setenv("PATH", os.pathsep.join(["", str(first), "", str(second)]))

    assert cli._iter_path_candidates(("bash.exe", "bash")) == [str(second / "bash")]


def test_resolve_bash_on_windows_with_only_system_bash(monkeypatch, tmp_path) -> None:
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    (system32 / "bash.exe").write_text("")
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.delenv("BASH", raising=False)
    monkeypatch.setenv("PATH", str(system32))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    # The WSL stub is the only bash; bundled *.sh scripts cannot run under it.
    assert cli._resolve_bash() is None


def test_resolve_npm_on_windows_without_npm(monkeypatch) -> None:
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli._resolve_npm() is None


# --- service scripts (run through real bash) ---------------------------------


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_run_service_passes_subcommand_and_exit_code(tmp_path, capsys) -> None:
    script = _script(
        tmp_path / "svc.sh",
        f'echo "$1" >> "{tmp_path}/calls"; [ "$1" = stop ] && exit 3; exit 0',
    )

    assert cli._run_service(script, "start") == 0
    assert cli._run_service(script, "stop") == 3
    assert (tmp_path / "calls").read_text().split() == ["start", "stop"]

    assert cli._run_service(tmp_path / "missing.sh", "start") == 1
    assert f"error: {tmp_path / 'missing.sh'} not found" in capsys.readouterr().err


def test_run_service_requires_bash(monkeypatch, tmp_path, capsys) -> None:
    script = _script(tmp_path / "svc.sh", "exit 0")
    monkeypatch.setattr(cli, "_resolve_bash", lambda: None)

    assert cli._run_service(script, "start") == 1
    assert "bash is required to run claude-smart service scripts" in (
        capsys.readouterr().err
    )


def test_service_status_waits_for_a_cold_start(tmp_path) -> None:
    counter = tmp_path / "count"
    script = _script(
        tmp_path / "svc.sh",
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); echo $((n+1)) > "{counter}"\n'
        'if [ "$n" -lt 2 ]; then echo "not running"; '
        'else echo "running on http://localhost:9"; fi',
    )

    assert (
        cli._service_status(script, wait_ready_s=5.0) == "running on http://localhost:9"
    )
    assert counter.read_text().strip() == "3"


def test_service_status_reports_missing_script_and_blank_output(tmp_path) -> None:
    silent = _script(tmp_path / "silent.sh", "true")

    assert cli._service_status(tmp_path / "missing.sh") == "script missing"
    assert cli._service_status(silent, wait_ready_s=0.0) == "unknown"


def test_service_status_gives_up_at_deadline(tmp_path) -> None:
    script = _script(tmp_path / "svc.sh", 'echo "not running"')

    assert cli._service_status(script, wait_ready_s=0.0) == "not running"
