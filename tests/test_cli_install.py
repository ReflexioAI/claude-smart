"""Tests for Claude Code install bootstrap recovery paths."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest  # type: ignore[reportMissingImports]
from claude_smart import cli


def _installed_plugin(tmp_path: Path, version: str = "9.8.7") -> Path:
    root = (
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / version
    )
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    install = scripts / "smart-install.sh"
    install.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$PWD" > "$HOME/bootstrap-ran"\n'
    )
    install.chmod(install.stat().st_mode | stat.S_IXUSR)
    return root


def _isolate_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli, "_REFLEXIO_DIR", tmp_path / ".reflexio")
    monkeypatch.setattr(
        cli, "_INSTALL_FAILURE_MARKER", tmp_path / ".claude-smart" / "install-failed"
    )
    monkeypatch.setenv("HOME", str(tmp_path))


def test_bootstrap_claude_code_install_runs_installed_smart_install(
    monkeypatch,
    tmp_path: Path,
) -> None:
    plugin_root = _installed_plugin(tmp_path)
    _isolate_home(monkeypatch, tmp_path)

    ok, message = cli._bootstrap_claude_code_install()

    assert ok, message
    assert message == str(plugin_root)
    assert (tmp_path / "bootstrap-ran").read_text().strip() == str(plugin_root)
    assert (tmp_path / ".reflexio" / "plugin-root").resolve() == plugin_root


def test_bootstrap_claude_code_install_runs_smart_install_via_resolved_bash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    plugin_root = _installed_plugin(tmp_path)
    _isolate_home(monkeypatch, tmp_path)
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(cmd, cwd=None):
        calls.append(([str(part) for part in cmd], cwd))
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(cli, "_resolve_bash", lambda: "/bin/bash")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    ok, message = cli._bootstrap_claude_code_install()

    assert ok, message
    assert message == str(plugin_root)
    assert calls == [
        (
            ["/bin/bash", str(plugin_root / "scripts" / "smart-install.sh")],
            plugin_root,
        )
    ]


def test_bootstrap_claude_code_install_prefers_highest_cache_version(
    monkeypatch,
    tmp_path: Path,
) -> None:
    old_root = _installed_plugin(tmp_path, "0.2.31")
    new_root = _installed_plugin(tmp_path, "0.2.32")
    old_mtime = new_root.stat().st_mtime + 100
    old_root.touch()
    os.utime(old_root, (old_mtime, old_mtime))
    _isolate_home(monkeypatch, tmp_path)

    ok, message = cli._bootstrap_claude_code_install()

    assert ok, message
    assert message == str(new_root)
    assert (tmp_path / "bootstrap-ran").read_text().strip() == str(new_root)
    assert (tmp_path / ".reflexio" / "plugin-root").resolve() == new_root


def test_bootstrap_claude_code_install_reports_failure_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    plugin_root = _installed_plugin(tmp_path)
    install = plugin_root / "scripts" / "smart-install.sh"
    install.write_text(
        "#!/usr/bin/env bash\n"
        'mkdir -p "$HOME/.claude-smart"\n'
        "printf 'network unavailable\\n' > \"$HOME/.claude-smart/install-failed\"\n"
    )
    install.chmod(install.stat().st_mode | stat.S_IXUSR)
    _isolate_home(monkeypatch, tmp_path)

    ok, message = cli._bootstrap_claude_code_install()

    assert not ok
    assert message == "network unavailable"


def test_bootstrap_claude_code_install_refuses_real_plugin_root_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _installed_plugin(tmp_path)
    _isolate_home(monkeypatch, tmp_path)
    real_dir = tmp_path / ".reflexio" / "plugin-root"
    real_dir.mkdir(parents=True)
    sentinel = real_dir / "keep.txt"
    sentinel.write_text("do not delete")

    ok, message = cli._bootstrap_claude_code_install()

    assert not ok
    assert "refusing to replace non-symlink plugin-root" in message
    assert sentinel.read_text() == "do not delete"


def test_cmd_install_fails_when_dependency_bootstrap_fails(
    monkeypatch, tmp_path: Path
) -> None:
    _installed_plugin(tmp_path)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **kw: argparse.Namespace(returncode=0)
    )
    monkeypatch.setattr(
        cli, "_bootstrap_claude_code_install", lambda: (False, "uv failed")
    )

    rc = cli.cmd_install(argparse.Namespace(host="claude-code", source="unused"))

    assert rc == 1


def test_cmd_install_refresh_existing_uninstalls_and_retries(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    install_attempts = 0

    def fake_run(cmd, check=False):
        nonlocal install_attempts
        command = [str(part) for part in cmd]
        calls.append(command)
        if command[1:3] == ["plugin", "install"]:
            install_attempts += 1
            if install_attempts == 1:
                raise subprocess.CalledProcessError(returncode=17, cmd=command)
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli, "_configure_reflexio_setup", lambda **_kwargs: False)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli, "_bootstrap_claude_code_install", lambda: (True, str(tmp_path / "plugin"))
    )
    monkeypatch.setattr(cli, "_restore_publish_hooks_from_source", lambda _root: None)

    rc = cli.cmd_install(argparse.Namespace(host="claude-code", refresh_existing=True))

    assert rc == 0
    assert calls == [
        ["claude", "plugin", "marketplace", "add", str(cli._REPO_ROOT)],
        ["claude", "plugin", "install", cli._PLUGIN_SPEC],
        ["claude", "plugin", "uninstall", cli._PLUGIN_SPEC],
        ["claude", "plugin", "install", cli._PLUGIN_SPEC],
    ]


def test_install_setup_default_creates_local_env(monkeypatch, tmp_path: Path) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)

    read_only = cli._configure_reflexio_setup()

    assert read_only is False
    text = env_path.read_text()
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in text
    assert 'CLAUDE_SMART_READ_ONLY="0"' in text
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_install_setup_updates_existing_host_and_local_defaults(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        "# keep\n"
        "CLAUDE_SMART_HOST=codex\n"
        'CLAUDE_SMART_READ_ONLY="1"\n'
    )
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.setenv("CLAUDE_SMART_USE_LOCAL_EMBEDDING", "0")

    read_only = cli._configure_reflexio_setup(host="opencode")

    text = env_path.read_text()
    assert read_only is True
    assert "# keep" in text
    assert "CLAUDE_SMART_HOST=opencode" in text
    assert "CLAUDE_SMART_HOST=codex" not in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=0" in text
    assert 'CLAUDE_SMART_READ_ONLY="1"' in text


def test_opencode_install_persists_resolved_cli_path(monkeypatch, tmp_path: Path) -> None:
    runtime_env_path = tmp_path / ".claude-smart" / ".env"
    opencode = tmp_path / "bin" / "opencode"
    opencode.parent.mkdir()
    opencode.write_text("#!/bin/sh\nexit 0\n")
    opencode.chmod(0o755)
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", runtime_env_path)
    monkeypatch.setenv("PATH", str(opencode.parent))
    monkeypatch.setenv("CLAUDE_SMART_OPENCODE_PATH", "")

    added = cli._persist_opencode_path()

    assert added == ["CLAUDE_SMART_OPENCODE_PATH"]
    assert f'CLAUDE_SMART_OPENCODE_PATH="{opencode}"' in runtime_env_path.read_text()
    assert not (tmp_path / ".reflexio" / ".env").exists()
    assert os.environ["CLAUDE_SMART_OPENCODE_PATH"] == str(opencode)


def test_install_setup_reads_managed_reflexio_from_env(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        '# keep\nUNKNOWN=value\nREFLEXIO_URL="https://managed.example/"\n'
        'REFLEXIO_API_KEY="rflx-test-secret"\n'
    )
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)

    read_only = cli._configure_reflexio_setup()

    text = env_path.read_text()
    assert "# keep" in text
    assert "UNKNOWN=value" in text
    assert 'REFLEXIO_URL="https://managed.example/"' in text
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI" not in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING" not in text
    assert read_only is False
    out = capsys.readouterr().out
    assert "rflx-****cret" in out
    assert "rflx-test-secret" not in out


def test_install_setup_reads_read_only_from_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_API_KEY="rflx-test-secret"\nCLAUDE_SMART_READ_ONLY="1"\n'
    )
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)

    read_only = cli._configure_reflexio_setup()

    assert read_only is True


def test_install_setup_ignores_stale_url_without_api_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text('REFLEXIO_URL="https://managed.example/"\n')
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.setenv("REFLEXIO_URL", "https://stale.example/")

    read_only = cli._configure_reflexio_setup()

    assert read_only is False
    assert "REFLEXIO_URL" not in os.environ
    text = env_path.read_text()
    assert "REFLEXIO_URL" not in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in text


def test_install_parser_keeps_plain_install() -> None:
    args = cli._build_parser().parse_args(["install"])

    assert args.host == "claude-code"
    assert not hasattr(args, "source")


def test_install_parser_no_longer_accepts_managed_flags() -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["install", "--api-key", "rflx-key"])

    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["install", "--read-only"])


def test_read_only_prunes_publish_hooks_but_keeps_runtime_hooks(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    hooks_dir = plugin_root / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "command": 'bash "$_R/scripts/hook_entry.sh" claude-code session-start'
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "command": 'bash "$_R/scripts/hook_entry.sh" claude-code stop'
                                }
                            ]
                        }
                    ],
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "command": 'bash "$_R/scripts/hook_entry.sh" claude-code session-end'
                                },
                                {
                                    "command": 'bash "$_R/scripts/dashboard-service.sh" session-end'
                                },
                            ]
                        }
                    ],
                }
            }
        )
        + "\n"
    )
    (hooks_dir / "codex-hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "command": 'bash "$_R/scripts/hook_entry.sh" codex session-start'
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "command": '"/node" "/plugin/scripts/codex-hook.js" "hook" "stop"'
                                }
                            ]
                        }
                    ],
                }
            }
        )
        + "\n"
    )

    cli._prune_publish_hooks_for_read_only(plugin_root)

    claude_hooks = json.loads((hooks_dir / "hooks.json").read_text())["hooks"]
    codex_hooks = json.loads((hooks_dir / "codex-hooks.json").read_text())["hooks"]
    assert "Stop" not in claude_hooks
    assert "Stop" not in codex_hooks
    claude_commands = [
        hook["command"]
        for blocks in claude_hooks.values()
        for block in blocks
        for hook in block["hooks"]
    ]
    codex_commands = [
        hook["command"]
        for blocks in codex_hooks.values()
        for block in blocks
        for hook in block["hooks"]
    ]
    assert any("claude-code session-start" in command for command in claude_commands)
    assert any("dashboard-service.sh" in command for command in claude_commands)
    assert any("session-end" in command for command in claude_commands)
    assert any("codex session-start" in command for command in codex_commands)


def test_restore_publish_hooks_from_source_restores_pruned_manifest(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "plugin"
    hooks_dir = plugin_root / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text('{"hooks": {"SessionStart": []}}\n')

    cli._restore_publish_hooks_from_source(plugin_root)

    restored = json.loads((hooks_dir / "hooks.json").read_text())["hooks"]
    assert "Stop" in restored
    assert "SessionEnd" in restored


def test_update_parser_no_longer_accepts_api_key() -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["update", "--api-key", "rflx-key"])


def test_update_parser_accepts_host_codex() -> None:
    args = cli._build_parser().parse_args(["update", "--host", "codex"])

    assert args.host == "codex"


def test_cmd_update_claude_code_stops_services_then_installs(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_run_service(script, subcmd):
        calls.append((script.name, subcmd))
        return 0

    def fake_install(args):
        calls.append(("install", args.host))
        assert args.refresh_existing is True
        assert args.marker == "kept"
        return 0

    monkeypatch.setattr(cli, "_run_service", fake_run_service)
    monkeypatch.setattr(cli, "cmd_install", fake_install)

    rc = cli.cmd_update(argparse.Namespace(host="claude-code", marker="kept"))

    assert rc == 0
    assert calls == [
        ("dashboard-service.sh", "stop"),
        ("backend-service.sh", "stop"),
        ("install", "claude-code"),
    ]


def test_cmd_update_codex_stops_cache_services_then_installs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "codex-cache" / "0.2.41"
    calls: list[tuple[str, str]] = []

    def fake_run_service(script, subcmd):
        calls.append((script.name, subcmd))
        return 0

    def fake_install(args):
        calls.append(("install-codex", args.host))
        assert args.marker == "kept"
        return 0

    monkeypatch.setattr(cli, "_find_codex_plugin_root", lambda: plugin_root)
    monkeypatch.setattr(cli, "_run_service", fake_run_service)
    monkeypatch.setattr(cli, "cmd_install_codex", fake_install)

    rc = cli.cmd_update(argparse.Namespace(host="codex", marker="kept"))

    assert rc == 0
    assert calls == [
        ("dashboard-service.sh", "stop"),
        ("backend-service.sh", "stop"),
        ("install-codex", "codex"),
    ]


def test_cmd_update_reads_managed_reflexio_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="rflx-test-secret"\n'
    )
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **kw: argparse.Namespace(returncode=0)
    )

    rc = cli.cmd_update(argparse.Namespace())

    assert rc == 0
    text = env_path.read_text()
    assert 'REFLEXIO_URL="https://managed.example/"' in text
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in text
    assert "REFLEXIO_USER_ID=" not in text
