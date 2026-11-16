"""Publish literal settings through the real Node, Bash and Python readers."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import dotenv
import pytest
from claude_smart import env_config
from dotenv import dotenv_values


@pytest.fixture(scope="module", autouse=True)
def locked_dotenv_reader() -> None:
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "plugin/uv.lock").read_text(encoding="utf-8"))
    expected = next(
        package["version"]
        for package in lock["package"]
        if package["name"] == "python-dotenv"
    )
    distribution = importlib.metadata.distribution("python-dotenv")
    assert distribution.version == expected, (
        "Use the locked plugin runtime for reader qualification"
    )
    assert (
        Path(dotenv.__file__).resolve()
        == Path(str(distribution.locate_file("dotenv/__init__.py"))).resolve()
    )


@pytest.fixture(scope="module")
def compiled_reader(tmp_path_factory) -> tuple[str, Path]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Actual reader probe requires Node")
    root = Path(__file__).resolve().parents[1]
    compiler = root / "node_modules/typescript/bin/tsc"
    assert compiler.is_file(), "npm ci is required for dashboard contract tests"
    target = tmp_path_factory.mktemp("literal-settings-reader")
    result = subprocess.run(  # noqa: S603 — repository compiler/source and owned output directory.
        [
            node,
            str(compiler),
            "plugin/dashboard/lib/config-file.ts",
            "--outDir",
            str(target),
            "--module",
            "commonjs",
            "--target",
            "ES2022",
            "--esModuleInterop",
            "--skipLibCheck",
            "--strict",
            "--typeRoots",
            str(root / "node_modules/@types"),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reader = target / "config-file.js"
    assert reader.is_file()
    return node, reader


@pytest.mark.skipif(os.name == "nt", reason="Actual Bash reader qualification")
@pytest.mark.parametrize(
    "value",
    [
        " leading",
        "trailing ",
        '"quoted"',
        "'quoted'",
        "value # comment",
        "\ufeffowned",
        "owned\ufeff",
        "'literal\\q",
        r" C:\temp\owned ",
        "plain\\",
    ],
)
def test_published_literal_roundtrips_actual_readers(
    tmp_path: Path, value: str, compiled_reader: tuple[str, Path]
) -> None:
    node, reader = compiled_reader
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Actual reader probe requires Bash")
    home = tmp_path / "home"
    settings = home / ".claude-smart/.env"
    settings.parent.mkdir(parents=True)
    original = b"# preserve\r\nUNRELATED=opaque\\path\r\n"
    settings.write_bytes(original)
    key = "CLAUDE_SMART_CLI_PATH"
    env_config.update_settings({key: value}, {key: []}, settings)
    with settings.open("ab") as stream:
        stream.write(b"AFTER_SINGLE='owned-keep'\r\nAFTER_DOUBLE=\"owned-keep\"\r\n")
    assert settings.read_bytes().startswith(original)
    assert dotenv_values(settings)[key] == value
    assert dotenv_values(settings)["AFTER_SINGLE"] == "owned-keep"
    assert dotenv_values(settings)["AFTER_DOUBLE"] == "owned-keep"
    rows = settings.read_bytes().decode().split("\n")
    assert (
        next(
            pair[1]
            for row in rows
            if (pair := env_config.parse_env_line(row)) and pair[0] == key
        )
        == value
    )

    root = Path(__file__).resolve().parents[1]
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "TMPDIR")
        if name in os.environ
    }
    environment.update(HOME=str(home), USERPROFILE=str(home))
    shell = subprocess.run(  # noqa: S603 — actual repository reader, fixed script and owned HOME.
        [
            bash,
            "--noprofile",
            "--norc",
            "-c",
            '. "$1"; claude_smart_source_reflexio_env; printf "%s" "$CLAUDE_SMART_CLI_PATH"',
            "owned",
            str(root / "plugin/scripts/_lib.sh"),
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert shell.returncode == 0, shell.stderr
    assert shell.stdout == value
    script = (
        "const fs=require('node:fs'),m=require(process.argv[1]);"
        "const row=fs.readFileSync(process.argv[2],'utf8').split('\\n').map(m.parseLine).find(p=>p?.key===process.argv[3]);"
        "process.stdout.write(JSON.stringify(row.value));"
    )
    result = subprocess.run(  # noqa: S603 — actual repository Node reader and owned settings.
        [
            node,
            "-e",
            script,
            str(reader),
            str(settings),
            key,
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == value

    # Exercise the replacement branch as well as the first-append branch,
    # retaining actual quoted originals as the compare-and-set precondition.
    expected = [
        line.removesuffix("\r")
        for line in rows
        if (pair := env_config.parse_env_line(line)) and pair[0] == key
    ]
    env_config.update_settings({key: value}, {key: expected}, settings)
    assert settings.read_bytes().startswith(original)
    assert dotenv_values(settings)[key] == value
