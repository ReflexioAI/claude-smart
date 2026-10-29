"""OpenCode local-package install, replacement and removal over real temp trees."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest  # type: ignore[reportMissingImports]
from claude_smart import cli

_REAL_HOOKS = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "hooks.json"


def _write_source_repo(repo_root: Path, *, install_script: str | None = None) -> Path:
    """A minimal npm-package-shaped checkout: package.json + plugin runtime."""
    scripts = repo_root / "plugin" / "scripts"
    scripts.mkdir(parents=True)
    (repo_root / "package.json").write_text('{"name": "claude-smart"}\n')
    install = scripts / "smart-install.sh"
    install.write_text(
        install_script
        or '#!/usr/bin/env bash\nprintf \'%s\\n\' "$PWD" > "$HOME/bootstrap-ran"\n'
    )
    install.chmod(install.stat().st_mode | stat.S_IXUSR)
    (scripts / "backend-service.sh").write_text("#!/usr/bin/env bash\n")
    hooks = repo_root / "plugin" / "hooks"
    hooks.mkdir()
    shutil.copyfile(_REAL_HOOKS, hooks / "hooks.json")
    return repo_root


@pytest.fixture
def opencode_home(monkeypatch, tmp_path):
    """Point every module-level home path at tmp_path; returns the package dir."""
    repo_root = _write_source_repo(tmp_path / "repo")
    package_dir = tmp_path / ".claude-smart" / "opencode" / "claude-smart"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_REPO_ROOT", repo_root)
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", repo_root / "plugin")
    monkeypatch.setattr(cli, "_SCRIPTS_DIR", repo_root / "plugin" / "scripts")
    monkeypatch.setattr(cli, "_OPENCODE_LOCAL_PACKAGE_DIR", package_dir)
    monkeypatch.setattr(cli, "_REFLEXIO_DIR", tmp_path / ".reflexio")
    monkeypatch.setattr(cli, "_STATE_DIR", tmp_path / ".claude-smart")
    monkeypatch.setattr(
        cli, "_INSTALL_FAILURE_MARKER", tmp_path / ".claude-smart" / "install-failed"
    )
    monkeypatch.setattr(
        cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env"
    )
    return package_dir


def test_install_package_replaces_previous_copy_and_leaves_no_staging(
    opencode_home: Path,
) -> None:
    opencode_home.mkdir(parents=True)
    (opencode_home / "stale.txt").write_text("from an older release\n")

    assert cli._install_opencode_plugin_package() == opencode_home

    assert not (opencode_home / "stale.txt").exists()
    assert (opencode_home / "plugin" / "scripts" / "smart-install.sh").is_file()
    # Staging copy, previous-version backup and lock are all cleaned up.
    assert list(opencode_home.parent.iterdir()) == [opencode_home]


def test_install_package_in_place_only_verifies(
    monkeypatch, opencode_home: Path
) -> None:
    # Running from the installed package itself must not copy onto itself.
    monkeypatch.setattr(cli, "_REPO_ROOT", opencode_home)
    _write_source_repo(opencode_home)
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", opencode_home / "plugin")
    copies: list[object] = []
    monkeypatch.setattr(cli.shutil, "copytree", lambda *a, **k: copies.append(a))

    assert cli._install_opencode_plugin_package() == opencode_home
    assert copies == []


def test_verify_package_rejects_missing_files_and_mismatched_runtime(
    opencode_home: Path, tmp_path: Path
) -> None:
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(OSError, match="missing .*package.json"):
        cli._verify_opencode_plugin_package(incomplete)

    other = _write_source_repo(tmp_path / "other", install_script="#!/bin/sh\n")
    with pytest.raises(OSError, match="does not match the installed claude-smart"):
        cli._verify_opencode_plugin_package(other)


def test_install_lock_reclaims_a_stale_lock(monkeypatch, tmp_path: Path) -> None:
    package_root = tmp_path / "opencode" / "claude-smart"
    lock = package_root.parent / ".install.lock"
    lock.mkdir(parents=True)
    old = time.time() - cli._OPENCODE_PACKAGE_LOCK_STALE_SECONDS - 60
    os.utime(lock, (old, old))

    with cli._opencode_package_install_lock(package_root):
        assert lock.is_dir()
        assert lock.stat().st_mtime > old
    assert not lock.exists()


def test_install_lock_times_out_while_another_install_holds_it(
    monkeypatch, tmp_path: Path
) -> None:
    package_root = tmp_path / "opencode" / "claude-smart"
    lock = package_root.parent / ".install.lock"
    lock.mkdir(parents=True)
    monkeypatch.setattr(cli, "_OPENCODE_PACKAGE_LOCK_TIMEOUT_SECONDS", 0.2)

    with pytest.raises(OSError, match="timed out waiting for OpenCode package"):
        with cli._opencode_package_install_lock(package_root):
            pytest.fail("lock must not be acquired while held")
    # A live lock held by someone else is left alone.
    assert lock.is_dir()


def test_replace_package_restores_previous_copy_when_swap_fails(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "claude-smart"
    package_root.mkdir()
    (package_root / "keep.txt").write_text("previous\n")
    missing_stage = tmp_path / "never-staged"

    with pytest.raises(OSError):
        cli._replace_opencode_plugin_package(missing_stage, package_root)

    assert (package_root / "keep.txt").read_text() == "previous\n"
    assert [p.name for p in tmp_path.iterdir()] == ["claude-smart"]


def test_replace_package_installs_fresh_when_nothing_was_there(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "new.txt").write_text("new\n")
    package_root = tmp_path / "claude-smart"

    cli._replace_opencode_plugin_package(staged, package_root)

    assert (package_root / "new.txt").read_text() == "new\n"
    assert not staged.exists()


def test_bootstrap_runs_smart_install_and_prunes_publish_hooks_when_read_only(
    opencode_home: Path, tmp_path: Path
) -> None:
    ok, message = cli._bootstrap_opencode_install(read_only=True)

    assert ok, message
    assert message == str(opencode_home)
    plugin_root = opencode_home / "plugin"
    assert (tmp_path / "bootstrap-ran").read_text().strip() == str(plugin_root)
    assert (tmp_path / ".reflexio" / "plugin-root").resolve() == plugin_root
    hooks = json.loads((plugin_root / "hooks" / "hooks.json").read_text())["hooks"]
    commands = [
        hook["command"]
        for blocks in hooks.values()
        for b in blocks
        for hook in b["hooks"]
    ]
    assert "Stop" not in hooks  # its only hook publishes
    assert not any(cli._command_is_publish_hook(command) for command in commands)
    # Non-publishing SessionEnd service hooks survive the prune.
    assert len(hooks["SessionEnd"][0]["hooks"]) == 2
    # The pristine source manifest is untouched.
    source = json.loads(_REAL_HOOKS.read_text())
    assert "Stop" in source["hooks"]


def test_bootstrap_keeps_publish_hooks_when_not_read_only(
    opencode_home: Path,
) -> None:
    ok, _message = cli._bootstrap_opencode_install(read_only=False)

    assert ok
    hooks = json.loads((opencode_home / "plugin" / "hooks" / "hooks.json").read_text())
    assert "Stop" in hooks["hooks"]


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("#!/usr/bin/env bash\nexit 4\n", "smart-install.sh failed in "),
        (
            '#!/usr/bin/env bash\nmkdir -p "$HOME/.claude-smart"\n'
            "printf '  uv download failed \\nmore\\n' > "
            '"$HOME/.claude-smart/install-failed"\n',
            "uv download failed",
        ),
        (
            '#!/usr/bin/env bash\nmkdir -p "$HOME/.claude-smart"\n'
            ': > "$HOME/.claude-smart/install-failed"\n',
            "unknown error",
        ),
    ],
)
def test_bootstrap_reports_smart_install_failures(
    monkeypatch, opencode_home: Path, tmp_path: Path, script: str, expected: str
) -> None:
    (tmp_path / "repo" / "plugin" / "scripts" / "smart-install.sh").write_text(script)

    ok, message = cli._bootstrap_opencode_install(read_only=False)

    assert not ok
    assert message.startswith(expected)


def test_bootstrap_reports_missing_bash_and_unusable_plugin_root(
    monkeypatch, opencode_home: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "_resolve_bash", lambda: None)
    assert cli._bootstrap_opencode_install(read_only=False) == (
        False,
        "bash is required to bootstrap claude-smart dependencies",
    )

    real_dir = tmp_path / ".reflexio" / "plugin-root"
    shutil.rmtree(real_dir.parent)
    real_dir.mkdir(parents=True)
    ok, message = cli._bootstrap_opencode_install(read_only=False)
    assert not ok
    assert "refusing to replace non-symlink plugin-root" in message


def test_bootstrap_reports_package_install_errors(
    monkeypatch, opencode_home: Path
) -> None:
    def boom() -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_install_opencode_plugin_package", boom)

    assert cli._bootstrap_opencode_install(read_only=False) == (False, "disk full")


# --- uninstall / update ------------------------------------------------------


def test_uninstall_opencode_removes_entries_package_and_stops_its_services(
    monkeypatch, opencode_home: Path, tmp_path: Path, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    package_spec = opencode_home.resolve().as_uri()
    (project / "opencode.json").write_text(
        json.dumps({"plugin": ["other-plugin", package_spec, "claude-smart@1.0.0"]})
    )
    _write_source_repo(opencode_home)
    local_scripts = opencode_home / "plugin" / "scripts"
    (local_scripts / "dashboard-service.sh").write_text("")
    stopped: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        cli, "_run_service", lambda script, sub: stopped.append((script, sub)) or 0
    )

    assert cli.cmd_uninstall_opencode(argparse.Namespace(global_config=False)) == 0

    assert json.loads((project / "opencode.json").read_text()) == {
        "plugin": ["other-plugin"]
    }
    assert not opencode_home.exists()
    assert not opencode_home.parent.exists()  # emptied parent is removed too
    assert stopped == [
        (cli._DASHBOARD_SCRIPT, "stop"),
        (cli._BACKEND_SCRIPT, "stop"),
        (local_scripts / "dashboard-service.sh", "stop"),
        (local_scripts / "backend-service.sh", "stop"),
    ]
    out = capsys.readouterr().out
    assert (
        f"Removed claude-smart OpenCode plugin entries from {project / 'opencode.json'}"
        in out
    )
    assert "Restart OpenCode to apply." in out


def test_uninstall_opencode_without_entries_reports_no_change(
    monkeypatch, opencode_home: Path, tmp_path: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "opencode.json").write_text('{"plugin": ["other-plugin"]}\n')
    monkeypatch.setattr(cli, "_run_service", lambda *_a: 0)

    assert cli.cmd_uninstall_opencode(argparse.Namespace()) == 0

    assert "OpenCode config did not include claude-smart." in capsys.readouterr().out
    assert (tmp_path / "opencode.json").read_text() == '{"plugin": ["other-plugin"]}\n'


def test_uninstall_opencode_refuses_invalid_config(
    monkeypatch, opencode_home: Path, tmp_path: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "opencode.json").write_text('{"plugin": "not-a-list"}')
    opencode_home.mkdir(parents=True)
    monkeypatch.setattr(cli, "_run_service", lambda *_a: 0)

    assert cli.cmd_uninstall_opencode(argparse.Namespace()) == 1

    assert 'field "plugin" must be a JSON array' in capsys.readouterr().err
    # Nothing is deleted when the config cannot be rewritten safely.
    assert opencode_home.is_dir()


def test_update_opencode_stops_services_then_reinstalls_for_opencode(
    monkeypatch,
) -> None:
    stopped: list[Path] = []
    installs: list[argparse.Namespace] = []
    monkeypatch.setattr(
        cli, "_run_service", lambda script, _sub: stopped.append(script) or 0
    )
    monkeypatch.setattr(
        cli, "cmd_install_opencode", lambda args: installs.append(args) or 3
    )

    assert cli.cmd_update(argparse.Namespace(host="opencode", global_config=True)) == 3

    assert stopped == [cli._DASHBOARD_SCRIPT, cli._BACKEND_SCRIPT]
    assert installs[0].host == cli._HOST_OPENCODE
    assert installs[0].global_config is True
