"""~/.reflexio/plugin-root bookkeeping, session lookup and service scripts."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest  # type: ignore[reportMissingImports]
from claude_smart import cli


@pytest.fixture
def homes(monkeypatch, tmp_path):
    state = tmp_path / ".claude-smart"
    reflexio = tmp_path / ".reflexio"
    monkeypatch.setattr(cli, "_STATE_DIR", state)
    monkeypatch.setattr(cli, "_REFLEXIO_DIR", reflexio)
    monkeypatch.setattr(cli, "_OPENCODE_LOCAL_PACKAGE_DIR", state / "opencode" / "cs")
    monkeypatch.setattr(
        cli, "_CODEX_PLUGIN_CACHE_DIR", tmp_path / ".codex" / "cache" / "cs"
    )
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", tmp_path / "market")
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", state / ".env")
    return tmp_path


def _runtime(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "backend-service.sh").write_text("#!/bin/sh\n")
    return root


def _no_symlinks(monkeypatch) -> None:
    """Simulate a filesystem (e.g. Windows without dev mode) without symlinks."""

    def refuse(self, *_a, **_k):
        raise OSError("symlinks not permitted")

    monkeypatch.setattr(Path, "symlink_to", refuse)


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")


def test_force_plugin_root_falls_back_to_text_pointer(monkeypatch, homes) -> None:
    root = _runtime(homes / "plugin")
    _no_symlinks(monkeypatch)

    cli._force_plugin_root(root)

    reflexio = homes / ".reflexio"
    assert not (reflexio / "plugin-root").exists()
    assert (reflexio / "plugin-root.txt").read_text() == f"{root}\n"
    assert not cli._plugin_root_is_broken()

    shutil.rmtree(root)
    assert cli._plugin_root_is_broken()


def test_active_plugin_root_reads_text_pointer_like_node(monkeypatch, homes) -> None:
    root = _runtime(homes / "plugin")
    _no_symlinks(monkeypatch)
    cli._force_plugin_root(root)

    assert cli._active_plugin_root() == root


def test_force_plugin_root_replaces_a_stale_file_pointer(homes) -> None:
    root = _runtime(homes / "plugin")
    (homes / ".reflexio").mkdir()
    (homes / ".reflexio" / "plugin-root").write_text("left by an old installer")

    cli._force_plugin_root(root)

    # A symlink, or the plugin-root.txt pointer where symlinks are unavailable.
    assert cli._active_plugin_root() == root


def test_active_plugin_root_without_any_pointer(homes) -> None:
    assert cli._active_plugin_root() is None
    assert not cli._plugin_root_is_broken()
    (homes / ".reflexio").mkdir()
    (homes / ".reflexio" / "plugin-root.txt").write_text("   \n")
    assert cli._active_plugin_root() is None
    assert not cli._plugin_root_is_broken()


def test_dangling_symlink_counts_as_broken_and_falls_back_to_text(homes) -> None:
    gone = homes / "gone"
    fallback = _runtime(homes / "fallback")
    reflexio = homes / ".reflexio"
    reflexio.mkdir()
    _symlink(reflexio / "plugin-root", gone)
    (reflexio / "plugin-root.txt").write_text(str(fallback))

    assert cli._plugin_root_is_broken()
    assert cli._active_plugin_root() == fallback


def test_symlink_to_root_without_runtime_is_broken(homes) -> None:
    hollow = homes / "hollow"
    hollow.mkdir()
    (homes / ".reflexio").mkdir()
    _symlink(homes / ".reflexio" / "plugin-root", hollow)

    assert cli._plugin_root_is_broken()


def test_repair_removes_text_pointer_when_nothing_is_left(monkeypatch, homes) -> None:
    _no_symlinks(monkeypatch)
    reflexio = homes / ".reflexio"
    reflexio.mkdir()
    (reflexio / "plugin-root.txt").write_text(str(homes / "deleted-root"))

    cli._repair_plugin_root()

    assert not (reflexio / "plugin-root.txt").exists()


def test_repair_hands_host_to_remaining_runtime_without_symlinks(
    monkeypatch, homes
) -> None:
    _no_symlinks(monkeypatch)
    opencode_root = _runtime(homes / ".claude-smart" / "opencode" / "cs" / "plugin")
    reflexio = homes / ".reflexio"
    reflexio.mkdir()
    (reflexio / "plugin-root.txt").write_text(str(homes / "deleted-codex-root"))
    env = homes / ".claude-smart" / ".env"
    env.write_text("CLAUDE_SMART_HOST=codex\n")

    cli._repair_plugin_root(cli._HOST_CODEX)

    assert (reflexio / "plugin-root.txt").read_text() == f"{opencode_root}\n"
    assert cli.env_config.parse_env_line(env.read_text().strip()) == (
        "CLAUDE_SMART_HOST",
        "opencode",
    )


def test_repair_keeps_host_when_a_different_host_was_removed(homes) -> None:
    claude_root = _runtime(
        homes / ".claude-smart" / "claude-code" / "claude-smart" / "plugin"
    )
    reflexio = homes / ".reflexio"
    reflexio.mkdir()
    _symlink(reflexio / "plugin-root", claude_root)
    env = homes / ".claude-smart" / ".env"
    env.write_text("CLAUDE_SMART_HOST=opencode\n")

    cli._repair_plugin_root(cli._HOST_CODEX)

    assert env.read_text() == "CLAUDE_SMART_HOST=opencode\n"
    assert (reflexio / "plugin-root").resolve() == claude_root


def test_repair_without_env_file_leaves_link_alone(homes) -> None:
    root = _runtime(homes / "root")
    (homes / ".reflexio").mkdir()
    _symlink(homes / ".reflexio" / "plugin-root", root)

    cli._repair_plugin_root(cli._HOST_CODEX)

    assert (homes / ".reflexio" / "plugin-root").resolve() == root
    assert not (homes / ".claude-smart" / ".env").exists()


def test_host_for_plugin_root_maps_install_locations(homes) -> None:
    opencode = homes / ".claude-smart" / "opencode" / "cs" / "plugin"
    codex_cache = homes / ".codex" / "cache" / "cs" / "1.0.0"
    marketplace = homes / "market" / "plugins" / "claude-smart"
    for path in (opencode, codex_cache, marketplace):
        path.mkdir(parents=True)

    assert cli._host_for_plugin_root(opencode) == cli._HOST_OPENCODE
    assert cli._host_for_plugin_root(codex_cache) == cli._HOST_CODEX
    assert cli._host_for_plugin_root(marketplace) == cli._HOST_CODEX
    assert cli._host_for_plugin_root(homes / "anything") == cli._HOST_CLAUDE_CODE


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("0.2.10-rc.1", (0, 2, 10)),
        ("4.5.6+build7", (4, 5, 6)),
        ("1.2", None),
        ("a.b.c", None),
        ("1.2.x", None),
    ],
)
def test_semver_tuple(name: str, expected) -> None:
    assert cli._semver_tuple(Path(name)) == expected


def test_versioned_installs_sort_above_unversioned_ones(tmp_path) -> None:
    unversioned = tmp_path / "dev-build"
    old = tmp_path / "0.1.0"
    for path in (old, unversioned):
        path.mkdir()
    os.utime(old, (1, 1))  # older mtime must not matter against a version

    ordered = sorted([unversioned, old], key=cli._installed_plugin_sort_key)

    assert ordered == [unversioned, old]


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
