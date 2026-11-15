"""End-to-end guided CLI checks with isolated homes and fake host/runtime tools."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_install_scripts import (
    LIB,
    NODE_INSTALLER,
    REPO_ROOT,
    _fake_claude_code_package,
    _fake_claude_install_script,
    _install_opencode_tarball_fixture,
    _isolated_env,
    _write_executable,
    _write_fake_claude_installer,
    _write_fake_codex,
    node_bin,
)


def guide_env(tmp_path: Path) -> dict[str, str]:
    env = _isolated_env(tmp_path)
    env.pop("CLAUDE_SMART_INSTALL_CHILD", None)
    env.pop("CLAUDE_SMART_DEFER_SHARED_SERVICES", None)
    env.pop("CLAUDE_SMART_CLI_PATH", None)
    for key in ("REFLEXIO_URL", "REFLEXIO_API_KEY", "REFLEXIO_USER_ID"):
        env.pop(key, None)
    env["CLAUDE_SMART_BACKEND_AUTOSTART"] = "0"
    env["CLAUDE_SMART_DASHBOARD_AUTOSTART"] = "0"
    return env


def fake_guide(tmp_path: Path, *, delay: bool = False) -> tuple[Path, dict[str, str]]:
    package = _fake_claude_code_package(
        tmp_path,
        "#!/bin/sh\n"
        + ("sleep 6\n" if delay else "")
        + 'printf \'{"continue":true,"suppressOutput":true}\\n\'\n',
    )
    (package / "package.json").write_text('{"name":"claude-smart","version":"0.3.6"}\n')
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "claude", _fake_claude_install_script())
    _write_executable(fake_bin / "codex", "#!/bin/sh\nexit 0\n")
    env = guide_env(tmp_path)
    # Managed configuration discovery is read-only, but installer tests must
    # not contact a real managed account or depend on network availability.
    fetch_stub = tmp_path / "managed-fetch-stub.cjs"
    fetch_stub.write_text(
        "global.fetch = async () => ({ ok: true, json: async () => ({}) });\n"
    )
    env["NODE_OPTIONS"] = f'--require "{fetch_stub}"'
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return package / "bin" / "claude-smart.js", env


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("explicit_provider", [False, True])
@pytest.mark.parametrize(
    ("key", "value", "summary"),
    [
        (
            "CLAUDE_SMART_CLI_PATH",
            "/custom/router",
            "custom executable (/custom/router)",
        ),
        ("CLAUDE_SMART_USE_LOCAL_CLI", "0", "configured cloud LLM"),
    ],
)
def test_unattended_install_preserves_inherited_provider_overrides(
    tmp_path: Path,
    saved: bool,
    explicit_provider: bool,
    key: str,
    value: str,
    summary: str,
) -> None:
    installer, env = fake_guide(tmp_path)
    config_path = tmp_path / ".claude-smart" / ".env"
    if saved:
        config_path.parent.mkdir()
        config_path.write_text(
            'REFLEXIO_URL="http://localhost:8071"\n'
            'CLAUDE_SMART_LEARNING_PROVIDER="claude-code"\n'
            'CLAUDE_SMART_CLI_PATH="/saved/router"\n'
            'CLAUDE_SMART_USE_LOCAL_CLI="1"\n'
        )
    env[key] = value
    args = [
        node_bin(),
        str(installer),
        "install",
        "--host",
        "claude-code",
        "--backend",
        "local",
    ]
    if explicit_provider:
        args += ["--provider", "claude-code"]
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    settings = config_path.read_text()
    if explicit_provider:
        assert "Background learning: Claude Code CLI" in result.stdout
        assert f'{key}="{value}"' not in settings
        assert 'CLAUDE_SMART_USE_LOCAL_CLI="1"' in settings
    else:
        assert summary in result.stdout
        assert f'{key}="{value}"' in settings


def test_guided_coordinator_reports_partial_success_and_keeps_provider(
    tmp_path: Path,
) -> None:
    installer, env = fake_guide(tmp_path)
    # The minimal fixture can install Claude Code but deliberately lacks Codex's
    # required marketplace payload. The coordinator must still finish the first host.
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "install",
            "--host",
            "claude-code,codex",
            "--backend",
            "local",
            "--provider",
            "codex",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stderr
    assert "Claude Code: installed" in result.stdout
    assert "Codex: failed" in result.stdout
    assert "Background learning: Codex CLI" in result.stdout
    assert '"continue"' not in result.stdout
    config = (tmp_path / ".claude-smart" / ".env").read_text()
    assert 'CLAUDE_SMART_LEARNING_PROVIDER="codex"' in config
    logs = list((tmp_path / ".claude-smart" / "install-logs").glob("*.log"))
    assert len(logs) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in logs)
    assert any("missing" in path.read_text() for path in logs)
    assert (
        tmp_path / ".claude-smart" / "claude-code" / "claude-smart" / "plugin"
    ).is_dir()


def test_guided_install_progress_is_visible_while_worker_is_waiting(
    tmp_path: Path,
) -> None:
    installer, env = fake_guide(tmp_path, delay=True)
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "install",
            "--host",
            "claude-code",
            "--backend",
            "local",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Preparing Python and runtime dependencies" in result.stdout
    assert "Installing Claude Code integration… 5s" in result.stdout
    assert result.stdout.index(
        "Installing Claude Code integration"
    ) < result.stdout.index("Installation complete")


@pytest.mark.parametrize("hosts", ["claude-code", "claude-code,codex"])
def test_guided_uninstall_keeps_data_configuration_and_remaining_integration(
    tmp_path: Path,
    hosts: str,
) -> None:
    installer, env = fake_guide(tmp_path)
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text('CLAUDE_SMART_LEARNING_PROVIDER="claude-code"\n')
    data = tmp_path / ".reflexio" / "data"
    data.mkdir(parents=True)
    (data / "sentinel").write_text("learned memories")
    # Install a remaining root and a symlink to the root being removed.
    removed = state / "claude-code" / "claude-smart" / "plugin"
    remaining = (
        tmp_path
        / ".codex"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / "0.3.6"
    )
    for root in (removed, remaining):
        (root / "scripts").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
        (root / "scripts" / "smart-install.sh").write_text("#!/bin/sh\nexit 0\n")
        shutil.copy2(
            installer.parent.parent / "plugin" / "scripts" / "backend-service.sh",
            root / "scripts" / "backend-service.sh",
        )
    (tmp_path / ".reflexio" / "plugin-root").symlink_to(
        removed, target_is_directory=True
    )
    result = subprocess.run(
        [node_bin(), str(installer), "uninstall", "--host", hosts],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Claude Code: removed" in result.stdout
    assert not removed.exists()
    if hosts == "claude-code":
        assert remaining.is_dir()
        assert (tmp_path / ".reflexio" / "plugin-root").resolve() == remaining.resolve()
    else:
        assert "Codex: removed" in result.stdout
        assert not remaining.exists()
        assert not (tmp_path / ".reflexio" / "plugin-root").exists()
    assert (
        'CLAUDE_SMART_LEARNING_PROVIDER="claude-code"' in (state / ".env").read_text()
    )
    assert (data / "sentinel").read_text() == "learned memories"


@pytest.mark.parametrize("host", ["claude-code", "codex", "opencode"])
def test_saved_learning_provider_wins_over_hook_identity(
    tmp_path: Path, host: str
) -> None:
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text('CLAUDE_SMART_LEARNING_PROVIDER="codex"\n')
    env = guide_env(tmp_path)
    env["CLAUDE_SMART_HOST"] = host
    env["CLAUDE_SMART_LEARNING_PROVIDER"] = "claude-code"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{LIB}"; claude_smart_source_reflexio_env; printf "%s:%s" "$CLAUDE_SMART_HOST" "$CLAUDE_SMART_LEARNING_PROVIDER"',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == f"{host}:codex"
    # Execute the actual backend selection block against the loaded environment.
    source = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()
    block = source[source.index("learning_provider=") : source.index("\nSTATE_DIR=")]
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{LIB}"; claude_smart_source_reflexio_env; PLUGIN_ROOT=/fixture; {block}\nprintf "%s" "$CLAUDE_SMART_CLI_PATH"',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == ""


@pytest.mark.skipif(os.name == "nt", reason="PTY terminal fixtures require POSIX")
@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("command", ["install", "setup"])
@pytest.mark.parametrize("cancel_key", [b"\x1b", b"\x03", b"\x04"])
def test_terminal_cancel_before_install_makes_no_changes(
    tmp_path: Path, cancel_key: bytes, command: str, seeded: bool
) -> None:
    import pty

    original = b'REFLEXIO_URL="http://localhost:9000/"\nREFLEXIO_API_KEY="saved-secret"\nCLAUDE_SMART_READ_ONLY=1\nREFLEXIO_ROW_LIMIT_INTERACTIONS=0\n'
    original += b"CLAUDE_SMART_USE_LOCAL_CLI=0\nCLAUDE_SMART_USE_LOCAL_CLI=1\nCLAUDE_SMART_CLI_PATH=/first\nCLAUDE_SMART_CLI_PATH=/last\nCLAUDE_SMART_HOST=codex\nCLAUDE_SMART_HOST=claude-code\n"
    if seeded:
        for folder in (".claude-smart", ".reflexio"):
            (tmp_path / folder).mkdir()
            (tmp_path / folder / ".env").write_bytes(original)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "claude", "#!/bin/sh\nexit 0\n")
    env = guide_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [node_bin(), str(NODE_INSTALLER), command],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
    )
    os.close(slave)
    output = b""
    try:
        deadline = time.monotonic() + 10
        while b"Which coding agents" not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                output += os.read(master, 8192)
        assert b"Which coding agents" in output
        os.write(master, cancel_key)
        proc.wait(timeout=5)
        if seeded:
            for folder in (".claude-smart", ".reflexio"):
                assert (tmp_path / folder / ".env").read_bytes() == original
                assert sorted(path.name for path in (tmp_path / folder).iterdir()) == [
                    ".env"
                ]
        else:
            assert not (tmp_path / ".claude-smart").exists()
            assert not (tmp_path / ".reflexio").exists()
        assert not (tmp_path / "claude.log").exists()
        assert not (tmp_path / "backend-service.log").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        os.close(master)


def test_node_guide_contracts_are_in_pytest_ci() -> None:
    result = subprocess.run(
        [node_bin(), "--test", "tests/install-guide.test.js"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_final_uninstall_does_not_restart_services_from_a_surviving_npx_root(
    tmp_path: Path,
) -> None:
    installer, env = fake_guide(tmp_path)
    legacy_root = installer.parent.parent / "plugin"
    data = tmp_path / ".reflexio"
    data.mkdir()
    (data / "plugin-root").symlink_to(legacy_root, target_is_directory=True)
    result = subprocess.run(
        [node_bin(), str(installer), "uninstall", "--host", "claude-code"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "no integrations remain" in result.stdout
    assert "start" not in (tmp_path / "backend-service.log").read_text().splitlines()
    assert legacy_root.exists()


@pytest.mark.skipif(os.name == "nt", reason="PTY terminal fixtures require POSIX")
@pytest.mark.parametrize("command", ["install", "setup"])
@pytest.mark.parametrize("proceed", [False, True])
def test_managed_key_is_hidden_and_final_cancel_does_not_save_it(
    tmp_path: Path,
    proceed: bool,
    command: str,
) -> None:
    import pty

    installer, env = fake_guide(tmp_path)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [node_bin(), str(installer), command],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
    )
    os.close(slave)
    output = b""

    def answer_after(label: bytes, answer: bytes) -> None:
        nonlocal output
        deadline = time.monotonic() + 10
        while label not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                output += os.read(master, 8192)
        assert label in output, output.decode(errors="replace")
        os.write(master, answer)

    try:
        answer_after(b"Which coding agents", b"\x1b[B \r")
        answer_after(b"Where should", b"\x1b[B\r")
        answer_after(b"Managed Reflexio endpoint", b"\r")
        answer_after(b"API key (hidden)", b"secret-terminal-sentinel\r")
        answer_after(b"Allow claude-smart", b"\r")
        answer_after(b"Memory sharing scope", b"\r")
        answer_after(b"Proceed with installation", b"\r" if proceed else b"\x1b[B\r")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.1)[0]:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        assert proc.poll() is not None, output.decode(errors="replace")
        assert proc.returncode == 0, output.decode(errors="replace")
        assert b"secret-terminal-sentinel" not in output
        if proceed:
            assert b"Installation complete" in output
            assert b"Claude Code: installed" in output
            assert (
                'REFLEXIO_API_KEY="secret-terminal-sentinel"'
                in (tmp_path / ".claude-smart" / ".env").read_text()
            )
        else:
            assert b"Cancelled; no changes made" in output
            assert not (tmp_path / ".claude-smart").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        os.close(master)


@pytest.mark.parametrize("command", ["update", "setup"])
@pytest.mark.parametrize("legacy", [True, False])
def test_guided_command_preserves_managed_key_read_only_and_sharing(
    tmp_path: Path, legacy: bool, command: str
) -> None:
    installer, env = fake_guide(tmp_path)
    saved = tmp_path / (".reflexio" if legacy else ".claude-smart") / ".env"
    saved.parent.mkdir()
    saved.write_text(
        'REFLEXIO_API_KEY="secret-managed-sentinel"\nCLAUDE_SMART_READ_ONLY="1"\nREFLEXIO_USER_ID="custom-shared-user"\n'
    )
    result = subprocess.run(
        [node_bin(), str(installer), command, "--host", "claude-code"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Managed Reflexio" in result.stdout
    assert (
        "Memory storage and background learning run on Reflexio cloud" in result.stdout
    )
    assert "Cloud dashboard: https://www.reflexio.ai/dashboard" in result.stdout
    assert "read-only" in result.stdout
    assert "secret-managed-sentinel" not in result.stdout
    config = (tmp_path / ".claude-smart" / ".env").read_text()
    assert 'REFLEXIO_API_KEY="secret-managed-sentinel"' in config
    assert 'CLAUDE_SMART_READ_ONLY="1"' in config
    assert 'REFLEXIO_USER_ID="custom-shared-user"' in config


def test_guided_update_keeps_claude_uninstall_and_retry_recovery(
    tmp_path: Path,
) -> None:
    installer, env = fake_guide(tmp_path)
    _write_executable(
        tmp_path / "fake-bin" / "claude",
        "#!/bin/sh\n"
        'printf "claude %s\\n" "$*" >> "$HOME/claude.log"\n'
        'if [ "$1 $2" = "plugin install" ] && [ ! -f "$HOME/tried-install" ]; then\n'
        '  touch "$HOME/tried-install"; exit 17\nfi\nexit 0\n',
    )
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "update",
            "--host",
            "claude-code",
            "--backend",
            "local",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "claude.log").read_text()
    assert calls.count("plugin install claude-smart@reflexioai") == 2
    assert "plugin uninstall claude-smart@reflexioai" in calls


@pytest.mark.parametrize("provider_mode", ["named", "custom", "cloud"])
def test_packaged_guide_installs_multiple_agents_and_reuses_saved_provider(
    tmp_path: Path,
    provider_mode: str,
) -> None:
    if not (REPO_ROOT / "plugin" / "vendor" / "reflexio").is_dir():
        pytest.skip("Release bundle required; generate the pinned vendor payload first")
    home, prefix, project, _xdg, env, setup = _install_opencode_tarball_fixture(
        tmp_path
    )
    assert setup.returncode == 0, setup.stderr
    _write_fake_claude_installer(tmp_path / "fake-bin" / "claude")
    _write_fake_codex(tmp_path / "fake-bin" / "codex")
    # This fixture's private node only records version probes; the fake Codex
    # app-server must run under the real test interpreter.
    codex = tmp_path / "fake-bin" / "codex"
    codex.write_text(
        codex.read_text().replace("#!/usr/bin/env node", f"#!{node_bin()}", 1)
    )
    env.pop("CLAUDE_SMART_INSTALL_CHILD", None)
    config_path = home / ".claude-smart" / ".env"
    config_path.unlink()
    provider_args = ["--provider", "codex"]
    if provider_mode == "custom":
        env["CLAUDE_SMART_CLI_PATH"] = "/custom/router"
        provider_args = []
    elif provider_mode == "cloud":
        env["CLAUDE_SMART_USE_LOCAL_CLI"] = "0"
        provider_args = []
    expected_learning = {
        "named": "Codex CLI",
        "custom": "custom executable (/custom/router)",
        "cloud": "configured cloud LLM",
    }[provider_mode]
    installer = prefix / "bin" / "claude-smart"
    result = subprocess.run(
        [
            str(installer),
            "install",
            "--host",
            "claude-code,codex",
            "--backend",
            "local",
            "--url",
            "http://localhost:9090/",
            "--sharing",
            "global",
        ]
        + provider_args,
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Claude Code: installed" in result.stdout
    assert "Codex: installed" in result.stdout
    assert f"Background learning: {expected_learning}" in result.stdout
    assert '"continue"' not in result.stdout
    settings = config_path.read_text()
    assert 'CLAUDE_SMART_GUIDED_CONFIG="1"' in settings
    assert 'REFLEXIO_URL="http://localhost:9090/"' in settings
    assert 'REFLEXIO_USER_ID="global_user"' in settings
    if provider_mode == "named":
        assert 'CLAUDE_SMART_LEARNING_PROVIDER="codex"' in settings
    else:
        assert "CLAUDE_SMART_LEARNING_PROVIDER=" not in settings
    env.pop("CLAUDE_SMART_CLI_PATH", None)
    env.pop("CLAUDE_SMART_USE_LOCAL_CLI", None)
    version = json.loads((REPO_ROOT / "package.json").read_text())["version"]
    cache = (
        home / ".codex" / "plugins" / "cache" / "reflexioai" / "claude-smart" / version
    )
    assert (cache / "hooks" / "codex-hooks.json").is_file()
    assert not (cache / ".venv").exists()
    runtime = home / ".codex" / "claude-smart" / "runtime" / "plugin"
    assert (runtime / ".venv" / "bin" / "python").is_file()
    assert (
        home
        / ".claude-smart"
        / "claude-code"
        / "claude-smart"
        / "plugin"
        / ".venv"
        / "bin"
        / "python"
    ).is_file()
    assert "hash-session-start" in (home / ".codex" / "config.toml").read_text()
    result = subprocess.run(
        [str(installer), "update", "--host", "claude-code"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Background learning: {expected_learning}" in result.stdout
    settings = config_path.read_text()
    assert 'REFLEXIO_URL="http://localhost:9090/"' in settings
    assert 'REFLEXIO_USER_ID="global_user"' in settings


@pytest.mark.parametrize("provider", [None, "claude-code", "codex", "opencode"])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("action", ["backend", "hook"])
def test_actual_codex_node_hook_uses_selected_learning_provider(
    tmp_path: Path, provider: str | None, override: bool, action: str
) -> None:
    from tests.test_install_scripts import CODEX_HOOK

    root = tmp_path / "plugin"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text(
        f'CLAUDE_SMART_LEARNING_PROVIDER="{provider}"\n' if provider else ""
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "claude", "#!/bin/sh\nexit 0\n")
    source = (REPO_ROOT / "plugin/scripts/backend-service.sh").read_text()
    selection = source[
        source.index("learning_provider=") : source.index("\nSTATE_DIR=")
    ]
    # Run the real Node entrypoint and the actual shell selection block through
    # both child routes; keep service/model startup entirely inside the fixture.
    recorder = (
        "#!/bin/bash\n"
        f'. "{LIB}"\nclaude_smart_source_reflexio_env\n'
        'printf "%s\\n" "$CLAUDE_SMART_HOST" > "$HOME/hook-identity"\n'
        f'PLUGIN_ROOT="{root}"\n{selection}\n'
        'printf "%s\\n" "$CLAUDE_SMART_CLI_PATH" > "$HOME/selected-cli"\n'
        'printf "%s\\n" "$CLAUDE_SMART_HOST" > "$HOME/learning-host"\n'
        "printf '{\"continue\":true}\\n'\n"
    )
    _write_executable(scripts / "backend-service.sh", recorder)
    prepared_python = root / ".venv" / "bin" / "python"
    prepared_python.parent.mkdir(parents=True)
    _write_executable(prepared_python, recorder)
    _write_executable(fake_bin / "uv", "#!/bin/sh\nexit 99\n")
    env = guide_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CLAUDE_PLUGIN_ROOT"] = str(root)
    env["CLAUDE_SMART_LEARNING_PROVIDER"] = "codex" if provider else ""
    if override:
        env["CLAUDE_SMART_CLI_PATH"] = "/explicit/learning-cli"
    args = [node_bin(), str(CODEX_HOOK), action]
    if action == "hook":
        args.append("post-tool")
    result = subprocess.run(
        args, input="{}", env=env, text=True, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    expected = (
        "/explicit/learning-cli"
        if override
        else str(fake_bin / "claude")
        if provider == "claude-code"
        else str(scripts / "opencode-claude-compat")
        if provider == "opencode"
        else ""
    )
    assert (tmp_path / "selected-cli").read_text().strip() == expected
    assert (tmp_path / "hook-identity").read_text().strip() == "codex"
    assert (tmp_path / "learning-host").read_text().strip() == (provider or "codex")


@pytest.mark.parametrize("command", ["install", "update", "setup"])
def test_learning_summary_and_sonnet_default_on_actual_guide(
    tmp_path: Path, command: str
) -> None:
    installer, env = fake_guide(tmp_path)
    claude = tmp_path / "fake-bin" / "claude"
    original = claude.read_text()
    claude.write_text(
        original.replace(
            "#!/bin/sh\n",
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "2.0.0 (Claude Code)"; exit 0; fi\n',
            1,
        )
    )
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            command,
            "--host",
            "claude-code",
            "--backend",
            "local",
            "--provider",
            "claude-code",
            "--yes",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = result.stdout.split("Installation complete", 1)[1]
    assert "Claude-smart version: 0.3.6" in summary
    assert "Background learning: Claude Code CLI" in summary
    assert "Provider CLI version: 2.0.0 (Claude Code)" in summary
    assert "Learning model: sonnet (Claude Code resolves the Sonnet alias)" in summary
    assert "Model version: not reported" in summary
    assert "npx claude-smart setup" in summary
    assert (
        'CLAUDE_SMART_LEARNING_MODEL="sonnet"'
        in (tmp_path / ".claude-smart/.env").read_text()
    )


@pytest.mark.parametrize(
    "provider,explicit,use_local_cli,expected",
    [
        ("claude-code", "", "1", "sonnet"),
        ("claude-code", "chosen-model", "1", "chosen-model"),
        ("claude-code", "", "0", ""),
        ("codex", "", "1", ""),
        ("opencode", "", "1", ""),
    ],
)
def test_backend_learning_model_selection(
    tmp_path: Path, provider: str, explicit: str, use_local_cli: str, expected: str
) -> None:
    source = (REPO_ROOT / "plugin/scripts/backend-service.sh").read_text()
    block = source[source.index("learning_provider=") : source.index("\nSTATE_DIR=")]
    env = guide_env(tmp_path)
    env["CLAUDE_SMART_LEARNING_PROVIDER"] = provider
    env["CLAUDE_SMART_LEARNING_MODEL"] = explicit
    env["CLAUDE_SMART_USE_LOCAL_CLI"] = use_local_cli
    env.pop("CLAUDE_SMART_CLI_MODEL", None)
    env.pop("CLAUDE_SMART_CLI_PATH", None)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{LIB}"; PLUGIN_ROOT=/fixture; {block}\nprintf "%s:%s" "${{CLAUDE_SMART_CLI_MODEL:-}}" "${{CLAUDE_SMART_LEARNING_MODEL:-}}"',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == f"{expected}:{expected}"


@pytest.mark.parametrize("arguments", [[], ["--help"], ["--backend", "local"]])
@pytest.mark.parametrize("overrides", [False, True])
def test_private_setup_refuses_before_configuration_or_installation(
    tmp_path, arguments, overrides
):
    installer, env = fake_guide(tmp_path)
    env["CLAUDE_SMART_INSTALL_CHILD"] = "1"
    if overrides:
        env.update(
            REFLEXIO_ENV_PATH=str(tmp_path / "alternate.env"),
            LEGACY_REFLEXIO_ENV_PATH=str(tmp_path / "legacy.env"),
            CLAUDE_SMART_SETUP_NO_INSTALL="1",
        )
    result = subprocess.run(
        [node_bin(), str(installer), "setup", *arguments],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert (
        "public guide" in result.stderr
        and "CLAUDE_SMART_INSTALL_CHILD" in result.stderr
    )
    assert not (tmp_path / ".claude-smart").exists()
    assert not (tmp_path / ".reflexio").exists()
    assert not (tmp_path / "alternate.env").exists()
    assert not (tmp_path / "claude.log").exists()
    assert not (tmp_path / "backend-service.log").exists()


@pytest.mark.parametrize(
    "scenario",
    [
        "fresh-local",
        "custom-local",
        "managed-to-local",
        "local-to-managed",
        "legacy-local-flags",
    ],
)
def test_public_setup_preserves_policy_and_connection_choices(tmp_path, scenario):
    from dotenv import dotenv_values

    installer, env = fake_guide(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    legacy = tmp_path / ".reflexio/.env"
    arguments = ["--backend", "local"]
    if scenario != "fresh-local":
        selected.parent.mkdir()
        selected.write_text(
            'CLAUDE_SMART_READ_ONLY="1"\nREFLEXIO_ROW_LIMIT_INTERACTIONS=0\nREFLEXIO_USER_ID="custom-shared-user"\n'
        )
    if scenario in {"custom-local", "local-to-managed"}:
        with selected.open("a") as target:
            target.write(
                'REFLEXIO_URL="http://localhost:9000/prefix/"\nREFLEXIO_API_KEY="local-key"\n'
            )
    elif scenario == "managed-to-local":
        legacy.parent.mkdir()
        legacy.write_text(
            'REFLEXIO_URL="https://legacy.example/"\nREFLEXIO_API_KEY="legacy-key"\n'
        )
        with selected.open("a") as target:
            target.write(
                'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="managed-key"\n'
            )
    elif scenario == "legacy-local-flags":
        legacy.parent.mkdir()
        legacy.write_text(
            'CLAUDE_SMART_READ_ONLY="0"\nCLAUDE_SMART_READ_ONLY="1"\nCLAUDE_SMART_CLI_TIMEOUT="90"\n'
        )
        selected.write_text("REFLEXIO_ROW_LIMIT_INTERACTIONS=0\n")
    if scenario == "local-to-managed":
        env["NEW_MANAGED_KEY"] = "new-managed-key"
        arguments = ["--backend", "managed", "--api-key-env", "NEW_MANAGED_KEY"]
    before_legacy = legacy.read_bytes() if legacy.exists() else None
    command = [
        node_bin(),
        str(installer),
        "setup",
        "--host",
        "claude-code",
        "--yes",
        *arguments,
    ]
    result = subprocess.run(
        command, env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = dotenv_values(selected)
    assert config["REFLEXIO_ROW_LIMIT_INTERACTIONS"] == (
        "500" if scenario == "fresh-local" else "0"
    )
    assert config["CLAUDE_SMART_READ_ONLY"] == (
        "0" if scenario == "fresh-local" else "1"
    )
    if scenario == "custom-local":
        assert config["REFLEXIO_URL"] == "http://localhost:9000/prefix/"
        assert config["REFLEXIO_API_KEY"] == "local-key"
    elif scenario == "local-to-managed":
        assert config["REFLEXIO_URL"] == "https://www.reflexio.ai/"
        assert config["REFLEXIO_API_KEY"] == "new-managed-key"
        assert config["CLAUDE_SMART_LOCAL_URL"] == "http://localhost:9000/prefix/"
        assert config["CLAUDE_SMART_LOCAL_API_KEY"] == "local-key"
    else:
        assert config["REFLEXIO_URL"] == "http://localhost:8071/"
        assert config["REFLEXIO_API_KEY"] == ""
    if scenario in {"custom-local", "managed-to-local", "local-to-managed"}:
        assert config["REFLEXIO_USER_ID"] == "custom-shared-user"
    if scenario == "legacy-local-flags":
        assert config["CLAUDE_SMART_CLI_TIMEOUT"] == "90"
    if before_legacy is not None:
        assert legacy.read_bytes() == before_legacy
    if scenario == "managed-to-local":
        assert config["CLAUDE_SMART_MANAGED_API_KEY"] == "managed-key"
        assert (selected.parent / "legacy-reflexio-env-checked").exists()
        again = subprocess.run(
            command, env=env, text=True, capture_output=True, timeout=30
        )
        assert again.returncode == 0, again.stderr
        assert dotenv_values(selected)["REFLEXIO_API_KEY"] == ""
    assert "local-key" not in result.stdout + result.stderr
    assert "new-managed-key" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "arguments", [["--unknown"], ["--backend"], ["--api-key", "never-on-argv"]]
)
def test_public_setup_invalid_arguments_preserve_saved_state(tmp_path, arguments):
    installer, env = fake_guide(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = b'REFLEXIO_URL="http://localhost:9000/"\nREFLEXIO_API_KEY="saved-key"\n'
    selected.write_bytes(original)
    result = subprocess.run(
        [node_bin(), str(installer), "setup", *arguments],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert selected.read_bytes() == original
    assert sorted(path.name for path in selected.parent.iterdir()) == [".env"]
    assert not (tmp_path / "claude.log").exists()
    assert not (tmp_path / "backend-service.log").exists()


def test_public_setup_passes_caller_workspace_to_real_host_child(tmp_path):
    installer, env = fake_guide(tmp_path)
    script = installer.parent.parent / "plugin/scripts/smart-install.sh"
    _write_executable(
        script,
        '#!/bin/sh\nprintf "%s" "$CLAUDE_SMART_DASHBOARD_WORKSPACE" > "$HOME/workspace"\nprintf \'{"continue":true,"suppressOutput":true}\\n\'\n',
    )
    project = tmp_path / "caller-project"
    project.mkdir()
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
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "workspace").read_text() == str(project)


@pytest.mark.parametrize("credential", ["", "   ", "\t"])
def test_setup_rejects_empty_supplied_managed_key_before_writes(tmp_path, credential):
    installer, env = fake_guide(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = (
        b'REFLEXIO_URL="http://localhost:9000/"\nREFLEXIO_API_KEY="saved-local-key"\n'
    )
    selected.write_bytes(original)
    env["EMPTY_MANAGED_KEY"] = credential
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "managed",
            "--api-key-env",
            "EMPTY_MANAGED_KEY",
            "--yes",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "requires a saved API key" in result.stderr
    assert selected.read_bytes() == original
    assert sorted(path.name for path in selected.parent.iterdir()) == [".env"]
    # Discovering available integrations is read-only and precedes key validation.
    assert (tmp_path / "claude.log").read_text().splitlines() == [
        "claude plugin list --json"
    ]
    assert not (tmp_path / "backend-service.log").exists()


@pytest.mark.skipif(os.name == "nt", reason="PTY terminal fixtures require POSIX")
@pytest.mark.parametrize(
    "runtime_kind",
    [
        "cloud",
        "custom",
        "native",
        "caller-custom",
        "caller-cloud",
        "caller-model",
        "duplicate-cloud",
        "duplicate-custom",
        "duplicate-host",
    ],
)
def test_interactive_setup_confirmation_preserves_effective_learning_intent(
    tmp_path, runtime_kind
):
    import pty
    import signal
    from dotenv import dotenv_values

    installer, env = fake_guide(tmp_path)
    for name in (
        "CLAUDE_SMART_LEARNING_MODEL",
        "CLAUDE_SMART_LEARNING_PROVIDER",
        "CLAUDE_SMART_USE_LOCAL_CLI",
        "CLAUDE_SMART_CLI_PATH",
    ):
        env.pop(name, None)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    saved_path = (
        "/saved/custom-native" if runtime_kind in {"custom", "duplicate-custom"} else ""
    )
    saved_enable = "0" if runtime_kind in {"cloud", "duplicate-cloud"} else "1"
    selected.write_text(
        'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_LEARNING_PROVIDER="codex"\nCLAUDE_SMART_LEARNING_MODEL="native/kept-model"\nCLAUDE_SMART_READ_ONLY="1"\nREFLEXIO_USER_ID="custom-shared-user"\nREFLEXIO_ROW_LIMIT_INTERACTIONS=0\n'
        + f'CLAUDE_SMART_CLI_PATH="{saved_path}"\nCLAUDE_SMART_USE_LOCAL_CLI="{saved_enable}"\n'
    )
    if runtime_kind == "duplicate-cloud":
        selected.write_text(selected.read_text() + 'CLAUDE_SMART_USE_LOCAL_CLI="1"\n')
    elif runtime_kind == "duplicate-custom":
        selected.write_text(
            selected.read_text() + 'CLAUDE_SMART_CLI_PATH="/later/custom"\n'
        )
    elif runtime_kind == "duplicate-host":
        selected.write_text(
            selected.read_text().replace(
                'CLAUDE_SMART_LEARNING_PROVIDER="codex"\n',
                'CLAUDE_SMART_HOST="codex"\nCLAUDE_SMART_HOST="claude-code"\n',
            )
        )
    expected_path, expected_enable, expected_model = (
        saved_path,
        saved_enable,
        "native/kept-model",
    )
    if runtime_kind == "caller-custom":
        env["CLAUDE_SMART_CLI_PATH"] = expected_path = "/caller/custom-native"
    elif runtime_kind == "caller-cloud":
        env["CLAUDE_SMART_USE_LOCAL_CLI"] = expected_enable = "0"
    elif runtime_kind == "caller-model":
        env["CLAUDE_SMART_LEARNING_MODEL"] = expected_model = "native/caller-model"
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [node_bin(), str(installer), "setup"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    os.close(slave)
    output = b""

    def answer_after(label, answer):
        nonlocal output
        deadline = time.monotonic() + 10
        while label not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                output += os.read(master, 8192)
        assert label in output, output.decode(errors="replace")
        os.write(master, answer)

    try:
        # Keep only Claude Code installed, while Codex remains the learning provider.
        answer_after(b"Which coding agents", b"\x1b[B \r")
        answer_after(b"Where should", b"\r")
        answer_after(b"Which agent should power background learning", b"\r")
        answer_after(b"Allow claude-smart", b"\r")
        assert b"Which model should" not in output
        answer_after(b"Memory sharing scope", b"\r")
        answer_after(b"Proceed with installation", b"\r")
        deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
        process.wait(timeout=5)
        assert process.returncode == 0, output.decode(errors="replace")
        if expected_path and expected_enable != "0":
            assert f"custom executable ({expected_path})".encode() in output
        config = dotenv_values(selected)
        assert config["CLAUDE_SMART_LEARNING_PROVIDER"] == "codex"
        assert config["CLAUDE_SMART_LEARNING_MODEL"] == expected_model
        assert config["CLAUDE_SMART_USE_LOCAL_CLI"] == expected_enable
        # Check the actual reader: dotenv_values would use the last duplicate,
        # while runtime caller-first execution flags retain the first nonempty.
        loaded = subprocess.run(
            [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                '. "$1"; claude_smart_source_reflexio_env; printf "%s" "$CLAUDE_SMART_CLI_PATH"',
                "reload",
                str(LIB),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert loaded.stdout == expected_path
        assert config["CLAUDE_SMART_READ_ONLY"] == "1"
        assert config["REFLEXIO_USER_ID"] == "custom-shared-user"
        assert config["REFLEXIO_ROW_LIMIT_INTERACTIONS"] == "0"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        os.close(master)
