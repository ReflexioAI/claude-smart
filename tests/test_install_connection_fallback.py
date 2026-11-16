"""Public setup keeps each saved endpoint with its own credential source."""

import json
import subprocess

import pytest
from dotenv import dotenv_values

from tests.test_install_guide import fake_guide
from tests.test_install_scripts import node_bin


def capture_discovery(tmp_path, env):
    # Record the public guide's managed-model request without any network access.
    capture = tmp_path / "fetch.json"
    preload = tmp_path / "capture.cjs"
    preload.write_text(
        'const fs = require("node:fs");\n'
        "global.fetch = async (url, options) => {\n"
        f"fs.appendFileSync({json.dumps(str(capture))}, JSON.stringify({{url: String(url), authorization: options.headers.authorization}}) + '\\n');\n"
        "return {ok: true, json: async () => ({})}; };\n"
    )
    env["NODE_OPTIONS"] = f'--require "{preload}"'
    return capture


@pytest.mark.parametrize(
    ("runtime", "backend", "expected_url", "expected_key"),
    [
        (
            "REFLEXIO_URL=http://localhost:9000/\n",
            "local",
            "http://localhost:9000/",
            "",
        ),
        (
            "REFLEXIO_API_KEY=runtime-canary\n",
            "managed",
            "https://www.reflexio.ai/",
            "runtime-canary",
        ),
        ("REFLEXIO_API_KEY=\n", "local", "http://localhost:8071/", ""),
        ("REFLEXIO_URL=\n", "local", "http://localhost:8071/", ""),
        (
            "REFLEXIO_URL=http://localhost:9000/\nREFLEXIO_API_KEY=\n",
            "local",
            "http://localhost:9000/",
            "",
        ),
        (
            "REFLEXIO_URL=https://current.example/\nREFLEXIO_API_KEY=runtime-canary\n",
            "managed",
            "https://current.example/",
            "runtime-canary",
        ),
        ("", "managed", "https://legacy.example/", "legacy-canary"),
    ],
)
def test_public_setup_uses_one_connection_source(
    tmp_path, runtime, backend, expected_url, expected_key
):
    installer, env = fake_guide(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    selected.write_text(runtime)
    legacy = tmp_path / ".reflexio/.env"
    legacy.parent.mkdir()
    original = (
        "REFLEXIO_URL=https://legacy.example/\n"
        "REFLEXIO_API_KEY=legacy-canary\n"
        "REFLEXIO_USER_ID=legacy-user\n"
        "CLAUDE_SMART_READ_ONLY=1\n"
    )
    legacy.write_text(original)
    capture = capture_discovery(tmp_path, env)
    arguments = ["--provider", "claude-code"] if backend == "local" else []
    result = subprocess.run(  # noqa: S603 -- Fixed Node argv and owned fixture package.
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--yes",
            "--backend",
            backend,
            *arguments,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual = dotenv_values(selected)
    assert actual["REFLEXIO_URL"] == expected_url
    assert actual["REFLEXIO_API_KEY"] == expected_key
    assert actual["CLAUDE_SMART_READ_ONLY"] == "1"
    assert actual["REFLEXIO_USER_ID"] == ("legacy-user" if not runtime else "")
    if runtime:
        assert "legacy-canary" not in selected.read_text()
        if backend == "local":
            assert "CLAUDE_SMART_MANAGED_URL" not in actual
    if backend == "managed":
        assert [json.loads(line) for line in capture.read_text().splitlines()] == [
            {
                "url": expected_url + "api/get_config",
                "authorization": f"Bearer {expected_key}",
            }
        ]
    else:
        assert not capture.exists()
    assert legacy.read_text() == original
    assert "legacy-canary" not in result.stdout + result.stderr
    assert "runtime-canary" not in result.stdout + result.stderr


def test_incomplete_managed_runtime_does_not_borrow_legacy_key(tmp_path):
    installer, env = fake_guide(tmp_path)
    capture = capture_discovery(tmp_path, env)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = "REFLEXIO_URL=https://current.example/\n"
    selected.write_text(original)
    legacy = tmp_path / ".reflexio/.env"
    legacy.parent.mkdir()
    legacy.write_text(
        "REFLEXIO_URL=https://legacy.example/\nREFLEXIO_API_KEY=legacy-canary\n"
    )
    result = subprocess.run(  # noqa: S603 -- Fixed Node argv and owned fixture package.
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--yes",
            "--backend",
            "managed",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "requires a saved API key" in result.stderr
    assert selected.read_text() == original
    assert not (selected.parent / "legacy-reflexio-env-checked").exists()
    assert not capture.exists()
    assert all(
        line == "claude plugin list --json"
        for line in (tmp_path / "claude.log").read_text().splitlines()
    )
    assert not (selected.parent / "install-logs").exists()
