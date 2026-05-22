"""Tests for Claude Code install bootstrap recovery paths."""

from __future__ import annotations

import argparse
import stat
from pathlib import Path

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


def test_install_setup_default_seeds_only_local_flags(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)

    cli._configure_reflexio_setup(
        argparse.Namespace(api_key="", reflexio_url="https://www.reflexio.ai/")
    )

    text = env_path.read_text()
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in text
    assert "REFLEXIO_URL" not in text
    assert "REFLEXIO_API_KEY" not in text
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_install_setup_api_key_configures_managed_reflexio(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text("# keep\nUNKNOWN=value\n")
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)

    cli._configure_reflexio_setup(
        argparse.Namespace(
            api_key="rflx-test-secret",
            reflexio_url="https://managed.example/",
        )
    )

    text = env_path.read_text()
    assert "# keep" in text
    assert "UNKNOWN=value" in text
    assert 'REFLEXIO_URL="https://managed.example/"' in text
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI" not in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING" not in text
    assert env_path.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert "rflx-****cret" in out
    assert "rflx-test-secret" not in out


def test_install_parser_defaults_managed_url_only_when_api_key_is_present() -> None:
    args = cli._build_parser().parse_args(["install", "--api-key", "rflx-key"])

    assert args.api_key == "rflx-key"
    assert args.reflexio_url == "https://www.reflexio.ai/"
