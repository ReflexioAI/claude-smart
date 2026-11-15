"""Integration management has one owner: the npm installer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_smart import cli, env_config


@pytest.mark.parametrize("command", ["install", "update", "uninstall"])
def test_python_installer_commands_reject_before_loading_settings(
    monkeypatch, tmp_path, capsys, command
):
    def unexpected_load():
        raise AssertionError("Rejected commands must not load configuration")

    monkeypatch.setattr(env_config, "load_reflexio_env", unexpected_load)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(SystemExit) as error:
        cli.main([command, "--host", "codex", "--global"])
    assert error.value.code == 2
    assert f"npx claude-smart {command}" in capsys.readouterr().err
    assert not (tmp_path / ".claude-smart").exists()
    assert not (tmp_path / ".reflexio").exists()


@pytest.mark.parametrize("command", ["install", "update", "uninstall"])
def test_shell_rejects_legacy_installation_before_reexec_or_self_heal(
    tmp_path, command
):
    # There is deliberately no _lib.sh next to this copy. A rejection that
    # reaches dependency setup/source/reexec must fail this test.
    source = Path(__file__).resolve().parents[1] / "plugin/scripts/cli.sh"
    script = tmp_path / "cli.sh"
    script.write_bytes(source.read_bytes())
    environment = dict(os.environ)
    environment.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    environment.pop("BASH_ENV", None)
    environment.pop("ENV", None)
    result = subprocess.run(
        ["bash", str(script), command, "--host", "opencode"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert f"npx claude-smart {command}" in result.stderr
    assert not (tmp_path / ".claude-smart").exists()
    assert not (tmp_path / ".reflexio").exists()
