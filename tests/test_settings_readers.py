"""Actual shell readers retain the same BOM/export and quoting contract."""

from __future__ import annotations

import os
import shutil
import subprocess
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
