"""Actual shell readers retain the same BOM/export and quoting contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "key,value",
    [
        ("REFLEXIO_URL", "https://owned.example/"),
        ("CLAUDE_SMART_USE_LOCAL_CLI", "0"),
        ("CLAUDE_SMART_CLI_PATH", r"C:\Program Files\owned\cli.exe"),
    ],
)
@pytest.mark.parametrize("prefix", ["", "\ufeff"])
@pytest.mark.parametrize("export", ["", "export\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_shell_reads_bom_and_export_assignments(
    tmp_path: Path, key: str, value: str, prefix: str, export: str, newline: str
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash service reader requires Bash")
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_bytes(
        (prefix + export + key + "=" + value + newline).encode()
    )
    env = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "SystemRoot")
        if name in os.environ
    }
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    library = Path(__file__).resolve().parents[1] / "plugin/scripts/_lib.sh"
    result = subprocess.run(  # noqa: S603 — actual repository reader and fixed test script.
        [
            bash,
            "--noprofile",
            "--norc",
            "-c",
            '. "$1"; claude_smart_source_reflexio_env; printf "%s" "${!2}"',
            "owned",
            library.as_posix(),
            key,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == value


@pytest.mark.skipif(os.name == "nt", reason="Owned hook probe uses a POSIX executable")
@pytest.mark.parametrize("export", ["", "export ", "export\t"])
@pytest.mark.parametrize("prefix", ["", "\ufeff"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "key,value,inherited,expected",
    [
        (
            "REFLEXIO_URL",
            "https://owned.example/",
            "https://stale.example/",
            "https://owned.example/",
        ),
        ("REFLEXIO_API_KEY", "", "stale-owned-key", ""),
        ("CLAUDE_SMART_USE_LOCAL_CLI", "0", "", "0"),
        ("CLAUDE_SMART_USE_LOCAL_CLI", "0", "1", "1"),
        (
            "CLAUDE_SMART_CLI_PATH",
            r"C:\Program Files\owned\cli.exe",
            "",
            r"C:\Program Files\owned\cli.exe",
        ),
    ],
)
def test_codex_hook_reads_export_assignments_with_existing_precedence(
    tmp_path: Path,
    export: str,
    prefix: str,
    newline: str,
    key: str,
    value: str,
    inherited: str,
    expected: str,
) -> None:
    import json

    node = shutil.which("node")
    if node is None:
        pytest.skip("Actual Codex launcher requires Node")
    state = tmp_path / ".claude-smart"
    state.mkdir()
    settings = state / ".env"
    original = (
        prefix
        + export
        + key
        + "="
        + value
        + newline
        + "CLAUDE_SMART_LEARNING_PROVIDER=claude-code"
        + newline
    ).encode()
    settings.write_bytes(original)
    root = tmp_path / "owned-plugin"
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    (root / "pyproject.toml").write_text("# owned hook runtime\n")
    captured = tmp_path / "captured.json"
    python.write_text(
        f"#!{sys.executable}\nimport json, os\nfrom pathlib import Path\n"
        f"Path({str(captured)!r}).write_text(json.dumps(dict(os.environ)))\n"
        "print('{\"continue\":true}')\n"
    )
    python.chmod(0o700)
    env = {name: os.environ[name] for name in ("PATH", "LANG") if name in os.environ}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PLUGIN_ROOT=str(root))
    env[key] = inherited
    launcher = Path(__file__).resolve().parents[1] / "plugin/scripts/codex-hook.js"
    result = subprocess.run(  # noqa: S603 — actual launcher, owned runtime and HOME.
        [node, str(launcher), "hook", "session-start"],
        env=env,
        input="{}",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text()).get(key) == expected
    assert settings.read_bytes() == original


@pytest.mark.parametrize("export", ["", "export ", "export\t"])
@pytest.mark.parametrize("prefix", ["", "\ufeff"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_public_setup_reuses_exported_saved_policy(
    tmp_path: Path,
    export: str,
    prefix: str,
    newline: str,
) -> None:
    from tests.test_install_scripts import _run_public_setup

    state = tmp_path / ".claude-smart"
    state.mkdir()
    settings = state / ".env"
    values = {
        "REFLEXIO_URL": "http://localhost:8071/",
        "REFLEXIO_API_KEY": "",
        "CLAUDE_SMART_LEARNING_PROVIDER": "codex",
        "CLAUDE_SMART_USE_LOCAL_CLI": "0",
        "CLAUDE_SMART_READ_ONLY": "1",
    }
    settings.write_bytes(
        (
            prefix
            + newline.join(export + key + "=" + value for key, value in values.items())
            + newline
        ).encode()
    )
    result = _run_public_setup(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    from claude_smart.env_config import parse_env_line

    saved = dict(
        pair
        for line in settings.read_text().splitlines()
        if (pair := parse_env_line(line))
    )
    for key, value in values.items():
        assert saved[key] == value
