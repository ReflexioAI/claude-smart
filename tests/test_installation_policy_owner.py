"""Prepared host workers preserve caller/runtime policy instead of choosing it."""

import os
import subprocess

import pytest
from dotenv import dotenv_values

from tests.test_install_scripts import (
    _fake_claude_code_package,
    _fake_claude_install_script,
    _isolated_env,
    _run_fake_claude_code_install,
    _write_bootstrap_uv,
    _write_executable,
    node_bin,
)


@pytest.mark.parametrize(
    "field",
    [
        "CLAUDE_SMART_USE_LOCAL_CLI",
        "CLAUDE_SMART_HOST",
        "CLAUDE_SMART_BACKEND_AUTOSTART",
        "CLAUDE_SMART_DASHBOARD_AUTOSTART",
        "CLAUDE_SMART_CLI_PATH",
        "CLAUDE_SMART_OPENCODE_PATH",
        "CLAUDE_SMART_CLI_TIMEOUT",
        "CLAUDE_SMART_STATE_DIR",
    ],
)
@pytest.mark.parametrize("first", ["0", " ", ""])
def test_prepared_worker_uses_effective_flag_without_rewriting_duplicates(
    tmp_path, field, first
):
    package = _fake_claude_code_package(
        tmp_path, '#!/bin/sh\nprintenv "$FIXTURE_FIELD" > "$HOME/seen-flag"\nexit 7\n'
    )
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = 'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_READ_ONLY="0"\nCLAUDE_SMART_LEARNING_PROVIDER="codex"\n'
    original += f'{field}="{first}"\n{field}="selected"\n{field}=""\n{field}="later"\n'
    selected.write_text(original)
    result = _run_fake_claude_code_install(
        tmp_path,
        package,
        {
            "FIXTURE_FIELD": field,
            "CLAUDE_SMART_BACKEND_AUTOSTART": "",
            "CLAUDE_SMART_DASHBOARD_AUTOSTART": "",
        },
    )
    assert result.returncode != 0
    assert "smart-install.sh failed" in result.stderr
    assert (tmp_path / "seen-flag").read_text().removesuffix("\n") == (
        first or "selected"
    )
    assert selected.read_text() == original


@pytest.mark.parametrize(
    "policy",
    [
        None,
        "",
        'REFLEXIO_URL="http://localhost:8071/"\n',
        'REFLEXIO_URL="not-a-url"\nREFLEXIO_API_KEY=""\n',
        'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY=" "\n',
    ],
)
def test_private_worker_refuses_unprepared_policy_before_bootstrap(tmp_path, policy):
    package = _fake_claude_code_package(
        tmp_path, '#!/bin/sh\necho prepared > "$HOME/prepared"\n'
    )
    fake = tmp_path / "bin"
    fake.mkdir()
    _write_executable(fake / "claude", _fake_claude_install_script())
    selected = tmp_path / ".claude-smart/.env"
    if policy is not None:
        selected.parent.mkdir()
        selected.write_text(policy)
    env = _isolated_env(tmp_path)
    env["PATH"] = str(fake) + os.pathsep + env["PATH"]
    result = subprocess.run(  # noqa: S603 -- Node and repository fixture in an owned HOME.
        [node_bin(), str(package / "bin/claude-smart.js"), "install"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "run npx claude-smart setup first" in result.stderr
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / ".claude-smart/claude-code/claude-smart").exists()
    if policy is None:
        assert not selected.exists()
    else:
        assert selected.read_text() == policy


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("remote", [False, True])
def test_shared_preparation_repairs_only_missing_defaults_with_real_owner(
    tmp_path, host, remote
):
    from tests.test_update_lifecycle import _fixture, _run

    package, _root, uv, env = _fixture(tmp_path, host, installed=False)
    _write_bootstrap_uv(uv, mode="fresh")
    selected = tmp_path / ".claude-smart/.env"
    original = (
        'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="fake-key"\n'
        if remote
        else 'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\n'
    ) + 'CLAUDE_SMART_LEARNING_PROVIDER="codex"\n'
    selected.write_text(original)
    result = _run(tmp_path, package, host, env)
    assert result.returncode == 0, result.stderr
    text = selected.read_text()
    assert "CLAUDE_SMART_HOST" not in text
    assert text.startswith(original)
    if remote:
        assert text == original
    else:
        assert "REFLEXIO_ROW_LIMIT_INTERACTIONS=500" in text
        assert 'CLAUDE_SMART_READ_ONLY="0"' in text
        assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
        assert selected.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("provider", [None, "opencode"])
def test_successful_install_starts_stored_learning_target_not_coding_identity(
    tmp_path, provider
):
    import http.server
    import threading

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    package = _fake_claude_code_package(tmp_path, "#!/bin/sh\nexit 0\n")
    _write_executable(
        package / "plugin/scripts/backend-service.sh",
        '#!/bin/sh\nif [ "$1" = status ]; then echo "running on http://localhost:1"; else echo "$1:$CLAUDE_SMART_HOST" >> "$HOME/started-host"; fi\n',
    )
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = f'REFLEXIO_URL="http://localhost:{port}/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_READ_ONLY="0"\nCLAUDE_SMART_HOST=codex\nCLAUDE_SMART_HOST=claude-code\n'
    if provider:
        original += f'CLAUDE_SMART_LEARNING_PROVIDER="{provider}"\n'
    selected.write_text(original)
    try:
        result = _run_fake_claude_code_install(
            tmp_path, package, {"BACKEND_PORT": str(port)}
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0, result.stderr
    assert (
        f"start:{provider or 'codex'}"
        in (tmp_path / "started-host").read_text().splitlines()
    )
    assert selected.read_text() == original
    assert f"Backend healthy at http://localhost:{port}/" in result.stdout


@pytest.mark.parametrize("stored_provider", [None, "", "opencode"])
def test_managed_confirmation_preserves_independent_legacy_learning_intent(
    tmp_path, stored_provider
):
    from tests.test_install_guide import fake_guide

    installer, env = fake_guide(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = 'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_HOST=codex\nCLAUDE_SMART_HOST=claude-code\nCLAUDE_SMART_LEARNING_MODEL="kept/native-model"\nCLAUDE_SMART_CLI_PATH="/kept/custom"\nCLAUDE_SMART_OPENCODE_PATH="/kept/opencode"\nCLAUDE_SMART_USE_LOCAL_CLI=0\nCLAUDE_SMART_READ_ONLY=1\n'
    if stored_provider is not None:
        original += f'CLAUDE_SMART_LEARNING_PROVIDER="{stored_provider}"\n'
    selected.write_text(original)
    env["OWNED_MANAGED_KEY"] = "fake-secret"
    result = subprocess.run(  # noqa: S603 -- Node and repository fixture in an owned HOME.
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "managed",
            "--url",
            "https://managed.example/",
            "--api-key-env",
            "OWNED_MANAGED_KEY",
            "--yes",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    text = selected.read_text()
    assert dotenv_values(selected, interpolate=False)[
        "CLAUDE_SMART_LEARNING_PROVIDER"
    ] == (stored_provider or "codex")
    assert 'CLAUDE_SMART_LEARNING_MODEL="kept/native-model"' in text
    assert 'CLAUDE_SMART_CLI_PATH="/kept/custom"' in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI=0" in text
    assert [
        line for line in text.splitlines() if line.startswith("CLAUDE_SMART_HOST=")
    ] == ["CLAUDE_SMART_HOST=codex", "CLAUDE_SMART_HOST=claude-code"]
    assert "fake-secret" not in result.stdout + result.stderr
    assert "CLAUDE_SMART_GUIDED_CONFIG" not in text
