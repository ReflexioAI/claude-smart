"""Public setup and legacy migration retain the runtime's saved flag choices."""

import subprocess
from pathlib import Path

import pytest

from tests.test_install_guide import fake_guide
from tests.test_install_scripts import LIB, node_bin


def _reload(env: dict[str, str], keys: list[str]) -> list[str]:
    # Source the real runtime reader; this never executes a service or provider.
    script = '. "$1"; claude_smart_source_reflexio_env; shift; for key do printenv "$key" || :; done'
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script, "reload", str(LIB), *keys],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout.splitlines()


def _env(tmp_path: Path):
    installer, env = fake_guide(tmp_path)
    for key in list(env):
        if key.startswith("CLAUDE_SMART_") and key not in {
            "CLAUDE_SMART_BACKEND_AUTOSTART",
            "CLAUDE_SMART_DASHBOARD_AUTOSTART",
        }:
            env.pop(key)
    return installer, env


@pytest.mark.parametrize("source", ["runtime", "legacy"])
@pytest.mark.parametrize("prefix_empty", [False, True])
def test_public_setup_keep_retains_effective_duplicate_flags(
    tmp_path, source, prefix_empty
):
    from dotenv import dotenv_values

    installer, env = _env(tmp_path)
    selected = tmp_path / (
        ".claude-smart/.env" if source == "runtime" else ".reflexio/.env"
    )
    selected.parent.mkdir(parents=True)
    flags = {
        "CLAUDE_SMART_USE_LOCAL_CLI": "0",
        "CLAUDE_SMART_HOST": "codex",
        "CLAUDE_SMART_CLI_PATH": r"C:\native\first.exe",
        "CLAUDE_SMART_OPENCODE_PATH": r"C:\native\opencode.exe",
        "CLAUDE_SMART_CLI_TIMEOUT": "90",
        "CLAUDE_SMART_STATE_DIR": str(tmp_path / "selected-state"),
    }
    text = 'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_LEARNING_MODEL="kept/model"\n'
    for key, value in flags.items():
        if prefix_empty:
            text += f'{key}=""\n'
        text += f'{key}="{value}"\n{key}=""\n{key}="later-value"\n'
    text += 'CLAUDE_SMART_READ_ONLY="1"\nCLAUDE_SMART_READ_ONLY="0"\n'
    selected.write_text(text)
    # Legacy generation defaults do not make an absent connection managed.
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "local",
            "--yes",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    target = tmp_path / ".claude-smart/.env"
    saved = dotenv_values(target)
    assert saved["REFLEXIO_NATIVE_CLI_MODEL"] == ""
    for name in (
        "CLAUDE_SMART_LEARNING_PROVIDER",
        "CLAUDE_SMART_LEARNING_MODEL",
        "CLAUDE_SMART_USE_LOCAL_CLI",
    ):
        assert name not in saved
    # Retired learning choices do not reach hook environments. The independent
    # executable, timeout, state and coding-integration settings still do.
    keys = [
        key
        for key in flags
        if key not in {"CLAUDE_SMART_HOST", "CLAUDE_SMART_USE_LOCAL_CLI"}
    ]
    assert _reload(env, keys) == [flags[key] for key in keys]
    assert saved["CLAUDE_SMART_READ_ONLY"] == "0"  # Shared fields still take last.
    if source == "legacy":
        assert selected.read_text() == text


@pytest.mark.parametrize("caller", ["", "0"])
def test_duplicate_autostart_uses_runtime_effective_opt_out(tmp_path, caller):
    installer, env = _env(tmp_path)
    for key in ("CLAUDE_SMART_BACKEND_AUTOSTART", "CLAUDE_SMART_DASHBOARD_AUTOSTART"):
        env[key] = caller
    target = tmp_path / ".claude-smart/.env"
    target.parent.mkdir()
    target.write_text(
        "CLAUDE_SMART_LEARNING_PROVIDER=codex\nCLAUDE_SMART_USE_LOCAL_CLI=0\n"
        "CLAUDE_SMART_BACKEND_AUTOSTART=0\nCLAUDE_SMART_BACKEND_AUTOSTART=1\n"
        "CLAUDE_SMART_DASHBOARD_AUTOSTART=0\nCLAUDE_SMART_DASHBOARD_AUTOSTART=1\n"
    )
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "local",
            "--yes",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "backend-service.log").exists()
    assert "autostart is disabled" in result.stdout.lower()


@pytest.mark.parametrize("value", ["", " ", "0"])
@pytest.mark.parametrize("destination_empty", [False, True])
def test_legacy_migration_preserves_present_empty_and_raw_values(
    tmp_path, value, destination_empty
):
    installer, env = _env(tmp_path)
    legacy = tmp_path / ".reflexio/.env"
    legacy.parent.mkdir()
    legacy.write_text(
        f'CLAUDE_SMART_CLI_TIMEOUT="{value}"\nCLAUDE_SMART_CLI_TIMEOUT=""\n'
    )
    target = tmp_path / ".claude-smart/.env"
    target.parent.mkdir()
    target.write_text(
        'CLAUDE_SMART_CLI_PATH=""\n'
        + ('CLAUDE_SMART_CLI_TIMEOUT=""\n' if destination_empty else "")
    )
    # Public confirmation owns migration; workers consume the confirmed policy.
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "local",
            "--yes",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    keys = ["CLAUDE_SMART_CLI_TIMEOUT", "CLAUDE_SMART_CLI_PATH"]
    expected = "" if destination_empty else value
    assert _reload(env, keys) == [expected, ""]
    assert f'CLAUDE_SMART_CLI_TIMEOUT="{expected}"' in target.read_text()


def test_public_setup_retains_last_shared_values_including_empty(tmp_path):
    installer, env = _env(tmp_path)
    env.update(
        REFLEXIO_URL="https://stale.invalid/",
        REFLEXIO_API_KEY="stale-key",
        REFLEXIO_USER_ID="stale-user",
        CLAUDE_SMART_READ_ONLY="1",
    )
    target = tmp_path / ".claude-smart/.env"
    target.parent.mkdir()
    target.write_text(
        'REFLEXIO_URL="https://previous.invalid/"\nREFLEXIO_URL="http://localhost:8071/"\n'
        'REFLEXIO_API_KEY="old-key"\nREFLEXIO_API_KEY=""\n'
        'REFLEXIO_USER_ID="old-user"\nREFLEXIO_USER_ID=""\n'
        'CLAUDE_SMART_LEARNING_PROVIDER="claude-code"\nCLAUDE_SMART_LEARNING_PROVIDER=""\n'
        'CLAUDE_SMART_HOST="codex"\nCLAUDE_SMART_HOST="claude-code"\n'
        'CLAUDE_SMART_LEARNING_MODEL="old-model"\nCLAUDE_SMART_LEARNING_MODEL=""\n'
        'CLAUDE_SMART_READ_ONLY="1"\nCLAUDE_SMART_READ_ONLY=""\n'
        'CLAUDE_SMART_USE_LOCAL_CLI="0"\nCLAUDE_SMART_USE_LOCAL_CLI="1"\n'
    )
    result = subprocess.run(
        [node_bin(), str(installer), "setup", "--host", "claude-code", "--yes"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Backend: Local Reflexio" in result.stdout
    keys = [
        "REFLEXIO_API_KEY",
        "REFLEXIO_USER_ID",
        "CLAUDE_SMART_READ_ONLY",
    ]
    assert _reload(env, keys) == ["", "", "0"]
    from dotenv import dotenv_values

    saved = dotenv_values(target, interpolate=False)
    assert saved["REFLEXIO_NATIVE_CLI_MODEL"] == ""
    assert "CLAUDE_SMART_LEARNING_MODEL" not in saved
    assert "CLAUDE_SMART_LEARNING_PROVIDER" not in saved
