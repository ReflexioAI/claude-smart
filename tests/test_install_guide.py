"""End-to-end guided CLI checks with isolated homes and fake host/runtime tools."""

# Contract subprocesses run repository scripts or fixture executables in owned homes.
# This is the same trusted-argv policy as the enterprise deploy-script tests.

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tests.test_install_scripts import (
    LIB,
    NODE_INSTALLER,
    REPO_ROOT,
    _copy_codex_install_sources,
    _fake_claude_code_package,
    _fake_claude_install_script,
    _install_opencode_tarball_fixture,
    _isolated_env,
    _write_bootstrap_uv,
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


def test_public_setup_preserves_a_concurrent_edit_after_preview(tmp_path):
    installer, env = fake_guide(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    selected.write_text(
        "REFLEXIO_URL=https://managed.example/\nREFLEXIO_API_KEY=preview-canary\n"
    )
    changed = "REFLEXIO_URL=https://managed.example/\nREFLEXIO_API_KEY=concurrent-canary\nOTHER=keep\n"
    stub = tmp_path / "change-after-preview.cjs"
    stub.write_text(
        "global.fetch=async()=>{require('node:fs').writeFileSync("
        + json.dumps(str(selected))
        + ","
        + json.dumps(changed)
        + ");return{ok:true,json:async()=>({})};};\n"
    )
    env["NODE_OPTIONS"] = f'--require "{stub}"'
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "managed",
            "--yes",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "changed after confirmation" in result.stderr
    assert selected.read_text() == changed
    assert not (selected.parent / "install-logs").exists()
    assert "preview-canary" not in result.stdout + result.stderr
    assert "concurrent-canary" not in result.stdout + result.stderr


def test_inconsistent_settings_bundle_refuses_before_preparation(tmp_path):
    installer, env = fake_guide(tmp_path)
    metadata = installer.parent.parent / "plugin/pyproject.toml"
    metadata.write_bytes(metadata.read_bytes() + b"\n# changed without regeneration\n")
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
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
        check=False,
    )
    assert result.returncode == 1
    assert "bundle is inconsistent" in result.stderr
    assert not (tmp_path / ".claude-smart/.env").exists()
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / ".claude-smart/install-logs").exists()


@pytest.mark.parametrize("fault", ["exit", "timeout"])
def test_lost_save_ack_stops_workers_and_retry_does_not_reimport_deleted_legacy(
    tmp_path,
    fault,
):
    installer, env = fake_guide(tmp_path)
    owner = installer.parent.parent / "plugin/src/claude_smart/env_config.py"
    original = owner.read_text()
    acknowledgement = '    print(json.dumps({"ok": True, "added": added}))'
    assert original.count(acknowledgement) == 1
    interruption = (
        "raise SystemExit(77)" if fault == "exit" else "__import__('time').sleep(30)"
    )
    if fault == "timeout":
        stub = tmp_path / "managed-fetch-stub.cjs"
        stub.write_text(
            stub.read_text()
            + "\nconst original=setTimeout;global.setTimeout=(fn,ms,...args)=>original(fn,ms===120000?2000:ms,...args);\n"
        )
    owner.write_text(
        original.replace(
            acknowledgement,
            '    if arguments and arguments[0] in {"publish-install", "publish-prepared-install"}:\n        '
            + interruption
            + "\n"
            + acknowledgement,
        )
    )
    legacy = tmp_path / ".reflexio/.env"
    legacy.parent.mkdir()
    legacy.write_text("CLAUDE_SMART_READ_ONLY=1\nCLAUDE_SMART_CLI_TIMEOUT=90\n")
    command = [
        node_bin(),
        str(installer),
        "setup",
        "--host",
        "claude-code",
        "--backend",
        "local",
        "--provider",
        "claude-code",
        "--yes",
    ]
    failed = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert failed.returncode == 1
    assert (
        "could not be confirmed"
        if fault == "exit"
        else "timed out and may have completed"
    ) in failed.stderr
    selected = tmp_path / ".claude-smart/.env"
    saved = selected.read_text()
    assert "# claude-smart: legacy-env checked" in saved.splitlines()
    assert dotenv_values(selected)["CLAUDE_SMART_CLI_TIMEOUT"] == "90"
    assert not (selected.parent / "install-logs").exists()
    assert not (selected.parent / "legacy-reflexio-env-checked").exists()
    selected.write_text(
        "".join(
            line
            for line in saved.splitlines(keepends=True)
            if not line.startswith("CLAUDE_SMART_CLI_TIMEOUT=")
        )
    )
    owner.write_text(original)
    retried = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert retried.returncode == 0, retried.stderr
    assert "CLAUDE_SMART_CLI_TIMEOUT" not in dotenv_values(selected)
    assert (
        selected.read_text().splitlines().count("# claude-smart: legacy-env checked")
        == 1
    )


def fake_guide(
    tmp_path: Path, *, wait_for_progress: bool = False
) -> tuple[Path, dict[str, str]]:
    package = _fake_claude_code_package(
        tmp_path,
        "#!/bin/sh\n"
        + "uv sync --locked --python 3.12 --quiet || exit $?\n"
        + '"$PWD/.venv/bin/python" -I -m claude_smart.env_config bootstrap-repair >/dev/null || exit $?\n'
        + 'printf \'{"continue":true,"suppressOutput":true}\\n\'\n',
    )
    (package / "package.json").write_text('{"name":"claude-smart","version":"0.3.6"}\n')
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "claude", _fake_claude_install_script())
    _write_executable(
        fake_bin / "codex",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "codex-cli ${CODEX_FIXTURE_VERSION:-0.148.0}"; fi\n'
        "exit 0\n",
    )
    _write_bootstrap_uv(fake_bin / "uv", mode="fresh")
    if wait_for_progress:
        uv = fake_bin / "uv"
        uv.write_text(
            uv.read_text().replace(
                "#!/bin/sh\n",
                "#!/bin/sh\n"
                'if [ "$1" = sync ]; then\n'
                "  i=0\n"
                '  while [ ! -e "$HOME/guide-progress" ]; do\n'
                '    [ "$i" -lt 40 ] || exit 97\n'
                "    sleep 0.25; i=$((i + 1))\n"
                "  done\n"
                "fi\n",
                1,
            )
        )
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


def test_setup_does_not_treat_completion_text_inside_a_value_as_metadata(tmp_path):
    installer, env = fake_guide(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    comment = "# claude-smart: legacy-env checked"
    opaque = 'OTHER="first\n' + comment + '\nlast"\n'
    selected.write_text(opaque)
    legacy = tmp_path / ".reflexio/.env"
    legacy.parent.mkdir()
    legacy.write_text("CLAUDE_SMART_CLI_TIMEOUT=90\n")
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "local",
            "--provider",
            "codex",
            "--yes",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    content = selected.read_text()
    assert content.startswith(comment + "\n")
    assert opaque in content
    assert (
        dotenv_values(stream=StringIO(content), interpolate=False)[
            "CLAUDE_SMART_CLI_TIMEOUT"
        ]
        == "90"
    )
    assert legacy.read_text() == "CLAUDE_SMART_CLI_TIMEOUT=90\n"


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
    if key == "CLAUDE_SMART_USE_LOCAL_CLI":
        # Supply deliberate cloud intent so this case reaches retired-input admission.
        env["REFLEXIO_NATIVE_CLI_MODEL"] = ""
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
    result = subprocess.run(
        args, env=env, text=True, capture_output=True, timeout=30, check=False
    )
    if key == "CLAUDE_SMART_USE_LOCAL_CLI":
        assert result.returncode != 0
        assert "obsolete learning selectors" in result.stderr
        return
    if not saved and not explicit_provider:
        assert result.returncode != 0
        assert "Choose a background learning provider" in result.stderr
        assert not config_path.exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    settings = config_path.read_text()
    if explicit_provider:
        assert "Background learning: Claude Code CLI" in result.stdout
        assert f'{key}="{value}"' not in settings
        assert (
            dotenv_values(stream=StringIO(settings), interpolate=False).get(
                "REFLEXIO_NATIVE_CLI_MODEL"
            )
            == "local-cli/claude-code/sonnet"
        )
    else:
        assert summary in result.stdout
        assert (
            dotenv_values(stream=StringIO(settings), interpolate=False).get(f"{key}")
            == f"{value}"
        )


def test_guided_coordinator_rejects_incomplete_package_before_registration(
    tmp_path: Path,
) -> None:
    installer, env = fake_guide(tmp_path)
    # The package lacks the requested Codex adapter. Preflight must fail
    # before the guide saves settings or registers either host.
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
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert (
        "published package is missing .agents/plugins/marketplace.json" in result.stderr
    )
    state = tmp_path / ".claude-smart"
    assert not (state / ".env").exists()
    assert not (state / "install-logs").exists()
    assert not (state / "claude-code" / "claude-smart" / "plugin").exists()


def test_codex_version_probe_ignores_unrelated_stderr(tmp_path: Path) -> None:
    installer, env = fake_guide(tmp_path)
    _write_executable(
        tmp_path / "fake-bin/codex",
        "#!/bin/sh\nprintf '%5000s\\n' 'startup diagnostics' >&2\n"
        "echo 'codex-cli 0.148.0'\n",
    )
    result = subprocess.run(
        [
            node_bin(),
            "-e",
            "console.log(require(process.argv[1]).learningProviderVersion({provider: 'codex'}))",
            str(installer),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "codex-cli 0.148.0"


def test_guided_install_reports_old_codex_and_installs_other_hosts(tmp_path: Path):
    installer, env = fake_guide(tmp_path)
    env["CODEX_FIXTURE_VERSION"] = "0.147.0"
    cli_dir = tmp_path / ".local/bin"
    cli_dir.mkdir(parents=True)
    (tmp_path / "fake-bin/claude").rename(cli_dir / "claude")
    hooks = installer.parents[1] / "plugin/hooks"
    hooks.mkdir(exist_ok=True)
    shutil.copyfile(REPO_ROOT / "plugin/hooks/hooks.json", hooks / "hooks.json")

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
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert "Claude Code: installed" in result.stdout, result.stderr
    assert "Codex: failed" in result.stdout
    assert "Codex CLI 0.148 or newer is required" in result.stdout
    state = tmp_path / ".claude-smart"
    assert not list((state / "install-logs").glob("install-codex-*.log"))
    assert (state / "guided-runtimes/selected").exists()
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()


def test_guided_install_with_old_codex_and_missing_opencode_keeps_settings(
    tmp_path: Path,
):
    installer, env = fake_guide(tmp_path)
    env["CODEX_FIXTURE_VERSION"] = "0.147.0"
    env["CLAUDE_SMART_OPENCODE_PATH"] = str(tmp_path / "missing" / "opencode")

    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "install",
            "--host",
            "codex,opencode",
            "--backend",
            "local",
            "--provider",
            "codex",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert "OpenCode CLI not found" in result.stderr
    assert "Codex CLI 0.148 or newer is required" in result.stderr
    assert not (tmp_path / ".claude-smart/.env").exists()


def test_guided_install_progress_is_visible_while_worker_is_waiting(
    tmp_path: Path,
) -> None:
    installer, env = fake_guide(tmp_path, wait_for_progress=True)
    gate = tmp_path / "release-on-progress.cjs"
    gate.write_text(r"""
const {writeFileSync} = require("node:fs");
const {join} = require("node:path");
if (process.env.CLAUDE_SMART_INSTALL_CHILD !== "1") {
  const write = process.stdout.write;
  process.stdout.write = function(chunk, ...args) {
    const result = write.call(this, chunk, ...args);
    if (/Preparing shared runtime… [0-9]+s/.test(String(chunk)))
      writeFileSync(join(process.env.HOME, "guide-progress"), "");
    return result;
  };
}
""")
    env["NODE_OPTIONS"] += f' --require "{gate}"'
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "install",
            "--host",
            "claude-code",
            "--backend",
            "local",
            "--provider",
            "claude-code",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Preparing Python and runtime dependencies" in result.stdout
    progress = re.search(r"Preparing shared runtime… ([0-9]+)s", result.stdout)
    assert progress, result.stdout
    assert int(progress[1]) >= 5
    assert progress.start() < result.stdout.index("Installation complete")


@pytest.mark.parametrize("hosts", ["claude-code", "claude-code,codex"])
@pytest.mark.parametrize(
    "policy", ["valid", "missing", "directory", "invalid", "invalid-utf8"]
)
def test_guided_uninstall_keeps_data_configuration_and_remaining_integration(
    tmp_path: Path,
    hosts: str,
    policy: str,
) -> None:
    installer, env = fake_guide(tmp_path)
    state = tmp_path / ".claude-smart"
    state.mkdir()
    selected = state / ".env"
    original = None
    if policy == "directory":
        selected.mkdir()
    elif policy != "missing":
        original = (
            b'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_LEARNING_PROVIDER="claude-code"\n'
            if policy in {"valid", "invalid-utf8"}
            else b'REFLEXIO_URL="invalid-endpoint"\nREFLEXIO_API_KEY=""\n'
        )
        if policy == "invalid-utf8":
            original += b"OPAQUE=\xff\n"
        selected.write_bytes(original)
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
        check=False,
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
    if policy == "missing":
        assert not selected.exists()
    elif policy == "directory":
        assert selected.is_dir()
    else:
        assert original is not None
        assert selected.read_bytes() == original
    if hosts == "claude-code" and policy != "valid":
        assert "Removal completed; repair saved settings" in result.stdout
        assert (
            "start" not in (tmp_path / "backend-service.log").read_text().splitlines()
        )
    assert (data / "sentinel").read_text() == "learned memories"


@pytest.mark.parametrize("host", ["claude-code", "codex", "opencode"])
def test_saved_native_target_is_not_exported_into_hook_identity(
    tmp_path: Path, host: str
) -> None:
    state = tmp_path / ".claude-smart"
    state.mkdir()
    original = 'REFLEXIO_NATIVE_CLI_MODEL="local-cli/codex/private-model"\n'
    (state / ".env").write_text(original)
    env = guide_env(tmp_path)
    env["CLAUDE_SMART_HOST"] = host
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{LIB}"; claude_smart_source_reflexio_env; printf "%s:%s" "$CLAUDE_SMART_HOST" "${{REFLEXIO_NATIVE_CLI_MODEL:-}}"',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == f"{host}:"
    assert (state / ".env").read_text() == original


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
        check=False,
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
        check=False,
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
        proc.wait(timeout=5)
        assert proc.poll() is not None, output.decode(errors="replace")
        assert proc.returncode == 0, output.decode(errors="replace")
        assert b"secret-terminal-sentinel" not in output
        if proceed:
            assert b"Installation complete" in output
            assert b"Claude Code: installed" in output
            assert (
                dotenv_values(
                    stream=StringIO((tmp_path / ".claude-smart" / ".env").read_text()),
                    interpolate=False,
                ).get("REFLEXIO_API_KEY")
                == "secret-terminal-sentinel"
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
        check=False,
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
    assert (
        dotenv_values(stream=StringIO(config), interpolate=False).get(
            "REFLEXIO_API_KEY"
        )
        == "secret-managed-sentinel"
    )
    assert (
        dotenv_values(stream=StringIO(config), interpolate=False).get(
            "CLAUDE_SMART_READ_ONLY"
        )
        == "1"
    )
    assert (
        dotenv_values(stream=StringIO(config), interpolate=False).get(
            "REFLEXIO_USER_ID"
        )
        == "custom-shared-user"
    )


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
            "--provider",
            "claude-code",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
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
    installer = prefix / "bin" / "claude-smart"
    removal = subprocess.run(
        [str(installer), "uninstall", "--host", "opencode", "--global", "--yes"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert removal.returncode == 0, removal.stdout + removal.stderr
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
    if provider_mode == "cloud":
        # Seed the retained standalone worker path to qualify upgrading an
        # older installation; public local setup now prepares a shared runtime.
        previous = subprocess.run(
            [str(installer), "install", "--host", "codex", "--yes"],
            cwd=project,
            env={**env, "CLAUDE_SMART_INSTALL_CHILD": "1"},
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert previous.returncode == 0, previous.stdout + previous.stderr
    config_path.unlink()
    provider_args = ["--provider", "codex", "--read-only"]
    if provider_mode == "custom":
        env["CLAUDE_SMART_CLI_PATH"] = "/custom/router"
        env["REFLEXIO_NATIVE_CLI_MODEL"] = "local-cli/codex"
        provider_args = []
    elif provider_mode == "cloud":
        env["REFLEXIO_NATIVE_CLI_MODEL"] = ""
        provider_args = []
    expected_learning = {
        "named": "Codex CLI",
        "custom": "custom executable (/custom/router)",
        "cloud": "configured cloud LLM",
    }[provider_mode]
    env["CLAUDE_SMART_DEFER_SHARED_SERVICES"] = "1"
    (home / "uv.log").unlink(missing_ok=True)
    hosts = (
        "claude-code,codex,opencode"
        if provider_mode == "named"
        else "claude-code,codex"
    )
    result = subprocess.run(
        [
            str(installer),
            "install",
            "--host",
            hosts,
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
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Claude Code: installed" in result.stdout
    assert "Codex: installed" in result.stdout
    assert f"Background learning: {expected_learning}" in result.stdout
    assert '"continue"' not in result.stdout
    settings = config_path.read_text()
    assert "CLAUDE_SMART_GUIDED_CONFIG" not in settings
    assert "CLAUDE_SMART_HOST=" not in settings
    assert (
        dotenv_values(stream=StringIO(settings), interpolate=False)["REFLEXIO_URL"]
        == "http://localhost:9090/"
    )
    assert (
        dotenv_values(stream=StringIO(settings), interpolate=False).get(
            "REFLEXIO_USER_ID"
        )
        == "global_user"
    )
    native_target = dotenv_values(stream=StringIO(settings), interpolate=False)[
        "REFLEXIO_NATIVE_CLI_MODEL"
    ]
    assert native_target == ("" if provider_mode == "cloud" else "local-cli/codex")
    assert "CLAUDE_SMART_LEARNING_PROVIDER=" not in settings
    assert "CLAUDE_SMART_USE_LOCAL_CLI=" not in settings
    env.pop("CLAUDE_SMART_CLI_PATH", None)
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    version = json.loads((REPO_ROOT / "package.json").read_text())["version"]
    cache = (
        home / ".codex" / "plugins" / "cache" / "reflexioai" / "claude-smart" / version
    )
    assert (cache / "hooks" / "codex-hooks.json").is_file()
    assert not (cache / ".venv").exists()
    (runtime,) = list((home / ".claude-smart/guided-runtimes").glob("runtime-*/plugin"))
    assert (runtime / ".venv/bin/python").is_file()
    assert (home / ".reflexio/plugin-root").resolve() == runtime.resolve()
    adapter = home / ".claude-smart/claude-code/claude-smart/plugin"
    assert not (adapter / ".venv").exists()
    standalone_codex = home / ".codex/claude-smart/runtime/plugin/.venv"
    assert standalone_codex.exists() == (provider_mode == "cloud")
    assert (home / "uv.log").read_text().count("uv sync --locked") == 1
    assert "uv venv" not in (home / "uv.log").read_text()
    claude_manifest = json.loads((adapter / "hooks/hooks.json").read_text())
    assert "Setup" not in claude_manifest["hooks"]
    assert str(runtime.resolve()) in json.dumps(claude_manifest)
    assert "ensure-plugin-root.sh" not in json.dumps(claude_manifest)
    assert str(runtime.resolve()) in (cache / "hooks/codex-hooks.json").read_text()
    loader = project / ".opencode/plugins/claude-smart.js"
    if provider_mode == "named":
        assert "Stop" not in claude_manifest["hooks"]
        codex_manifest = json.loads((cache / "hooks/codex-hooks.json").read_text())
        assert "Stop" not in codex_manifest["hooks"]
        assert "OpenCode: installed" in result.stdout
        assert (
            runtime.resolve() / "opencode/dist/server.mjs"
        ).as_uri() in loader.read_text()
    assert "hash-session-start" in (home / ".codex" / "config.toml").read_text()
    result = subprocess.run(
        [str(installer), "update", "--host", "claude-code"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Background learning: {expected_learning}" in result.stdout
    settings = config_path.read_text()
    assert (
        dotenv_values(stream=StringIO(settings), interpolate=False)["REFLEXIO_URL"]
        == "http://localhost:9090/"
    )
    assert (
        dotenv_values(stream=StringIO(settings), interpolate=False).get(
            "REFLEXIO_USER_ID"
        )
        == "global_user"
    )

    unrelated_project = tmp_path / "unrelated"
    unrelated_project.mkdir()
    if provider_mode == "named":
        original_loader = loader.read_text()
        loader.write_text(
            original_loader.replace(
                (runtime.resolve() / "opencode/dist/server.mjs").as_uri(),
                "file://remote.invalid/share/plugin/opencode/dist/server.mjs",
            )
        )
        status = subprocess.run(
            [str(installer), "status", "--host", "opencode"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert status.returncode == 0, status.stdout + status.stderr
        assert "Prepared runtime version: not installed" in status.stdout
        loader.write_text(original_loader)
        # A generated-looking loader in a different project confers no
        # account registration, even when it names a real prepared runtime.
        foreign_loader = unrelated_project / ".opencode/plugins/claude-smart.js"
        foreign_loader.parent.mkdir(parents=True)
        foreign_loader.write_text(original_loader)
        foreign = subprocess.run(
            [str(installer), "status", "--host", "opencode"],
            cwd=unrelated_project,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert foreign.returncode == 0, foreign.stdout + foreign.stderr
        assert "Prepared runtime version: not installed" in foreign.stdout
        foreign_loader.unlink()

    # A standalone update reselects its runtime. Removing it must recover
    # the still-registered shared runtime, not a host adapter without Python.
    surviving_hosts = {
        "cloud": ["claude-code"],  # Codex remains.
        "named": ["codex", "claude-code"],  # OpenCode remains.
    }
    for host in surviving_hosts.get(provider_mode, []):
        removal = subprocess.run(
            [str(installer), "uninstall", "--host", host, "--yes"],
            cwd=unrelated_project,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert removal.returncode == 0, removal.stdout + removal.stderr
    if provider_mode in surviving_hosts:
        assert (home / ".reflexio/plugin-root").resolve() == runtime.resolve()
        assert (runtime / ".venv/bin/python").is_file()

    if provider_mode == "named":
        # The selected generation still exists after uninstall. Recovery must
        # use live account registrations, including a different project's loader.
        next_install = subprocess.run(
            [str(installer), "install", "--host", "claude-code,codex", "--yes"],
            cwd=unrelated_project,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert next_install.returncode == 0, next_install.stdout + next_install.stderr
        link = home / ".reflexio/plugin-root"
        retired = link.resolve()
        assert retired != runtime.resolve()
        next_remove = subprocess.run(
            [str(installer), "uninstall", "--host", "claude-code,codex", "--yes"],
            cwd=unrelated_project,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert next_remove.returncode == 0, next_remove.stdout + next_remove.stderr
        assert retired.is_dir()
        assert link.resolve() == runtime.resolve()


@pytest.mark.parametrize("provider", [None, "claude-code", "codex", "opencode"])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("action", ["backend", "hook"])
def test_actual_codex_node_entrypoint_preserves_caller_context_and_cli_override(
    tmp_path: Path, provider: str | None, override: bool, action: str
) -> None:
    from tests.test_install_scripts import CODEX_HOOK

    root = tmp_path / "plugin"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    state = tmp_path / ".claude-smart"
    state.mkdir()
    original = f'REFLEXIO_NATIVE_CLI_MODEL="local-cli/{provider}"\n' if provider else ""
    (state / ".env").write_text(original)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "claude", "#!/bin/sh\nexit 0\n")
    # Exercise service dispatch and hook attribution through the real Node
    # entrypoint with a backend and interpreter confined to the test account.
    recorder = (
        "#!/bin/bash\n"
        f'. "{LIB}"\nclaude_smart_source_reflexio_env\n'
        'printf "%s\\n" "$CLAUDE_SMART_CLI_PATH" > "$HOME/selected-cli"\n'
        'printf "%s\\n" "$CLAUDE_SMART_HOST" > "$HOME/inherited-host"\n'
        "printf '{\"continue\":true}\\n'\n"
    )
    _write_executable(scripts / "backend-service.sh", recorder)
    prepared_python = root / ".venv" / "bin" / "python"
    prepared_python.parent.mkdir(parents=True)
    _write_executable(
        prepared_python,
        "#!/bin/sh\n"
        'printf "%s\\n" "$3" > "$HOME/hook-identity"\n'
        'printf "%s\\n" "$CLAUDE_SMART_HOST" > "$HOME/inherited-host"\n'
        'printf "%s\\n" "$CLAUDE_SMART_CLI_PATH" > "$HOME/selected-cli"\n'
        "printf '{\"continue\":true}\\n'\n",
    )
    _write_executable(fake_bin / "uv", "#!/bin/sh\nexit 99\n")
    env = guide_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CLAUDE_PLUGIN_ROOT"] = str(root)
    env["CLAUDE_SMART_HOST"] = "opencode"
    env.pop("CLAUDE_SMART_LEARNING_PROVIDER", None)
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    if override:
        env["CLAUDE_SMART_CLI_PATH"] = "/explicit/learning-cli"
    args = [node_bin(), str(CODEX_HOOK), action]
    if action == "hook":
        args.append("post-tool")
    result = subprocess.run(
        args,
        input="{}",
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = "/explicit/learning-cli" if override else ""
    assert (tmp_path / "selected-cli").read_text().strip() == expected
    if action == "hook":
        assert (tmp_path / "hook-identity").read_text().strip() == "codex"
    else:
        assert not (tmp_path / "hook-identity").exists()
    assert (tmp_path / "inherited-host").read_text().strip() == "opencode"
    assert (state / ".env").read_text() == original


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
        check=False,
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
        dotenv_values(
            stream=StringIO((tmp_path / ".claude-smart/.env").read_text()),
            interpolate=False,
        ).get("REFLEXIO_NATIVE_CLI_MODEL")
        == "local-cli/claude-code/sonnet"
    )


# The removed shell compiler's five string-projection cases are superseded
# by test_native_codex_learning's actual SDK dispatch/refusal/worker matrix.


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
        check=False,
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
    if scenario != "local-to-managed":
        arguments += ["--provider", "claude-code"]
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
        command, env=env, text=True, capture_output=True, timeout=30, check=False
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
        assert "# claude-smart: legacy-env checked" in selected.read_text().splitlines()
        assert not (selected.parent / "legacy-reflexio-env-checked").exists()
        again = subprocess.run(
            command, env=env, text=True, capture_output=True, timeout=30, check=False
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
        check=False,
    )
    assert result.returncode != 0
    assert selected.read_bytes() == original
    assert sorted(path.name for path in selected.parent.iterdir()) == [".env"]
    assert not (tmp_path / "claude.log").exists()
    assert not (tmp_path / "backend-service.log").exists()


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
        check=False,
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
        "CLAUDE_SMART_HOST",
        "REFLEXIO_NATIVE_CLI_MODEL",
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
        env["REFLEXIO_NATIVE_CLI_MODEL"] = ""
        expected_enable = "0"
    elif runtime_kind == "duplicate-host":
        # Coding integration history cannot authorize a learning host. The
        # caller explicitly resolves this otherwise ambiguous conversion.
        env["REFLEXIO_NATIVE_CLI_MODEL"] = "local-cli/codex/native/kept-model"
    elif runtime_kind == "caller-model":
        expected_model = "native/caller-model"
        env["REFLEXIO_NATIVE_CLI_MODEL"] = f"local-cli/codex/{expected_model}"
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            # Give the detached dependency/registration children a genuine
            # controlling terminal; inherited slave FDs alone lose writes on
            # macOS once a session-leader child exits.
            "import os,sys;os.login_tty(0);os.execv(sys.argv[1],sys.argv[1:])",
            node_bin(),
            str(installer),
            "setup",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
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
        assert config["REFLEXIO_NATIVE_CLI_MODEL"] == (
            "" if expected_enable == "0" else f"local-cli/codex/{expected_model}"
        )
        for retired in (
            "CLAUDE_SMART_LEARNING_PROVIDER",
            "CLAUDE_SMART_LEARNING_MODEL",
            "CLAUDE_SMART_USE_LOCAL_CLI",
        ):
            assert retired not in config
        # One-way conversion retains the effective and inactive old choices,
        # including duplicate assignments, as exact private recovery bytes.
        history = list((selected.parent / "native-selection-history").glob("*.env"))
        assert len(history) == 1
        assert (
            expected_model in history[0].read_text() or runtime_kind == "caller-model"
        )
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


@pytest.mark.parametrize("remembered", [False, True])
@pytest.mark.parametrize(
    ("supplied", "normalized"),
    [
        ("http://127.1:9090", "http://127.0.0.1:9090/"),
        ("http://LOCALHOST:9090", "http://localhost:9090/"),
        ("http://0x7f000001:9090", "http://127.0.0.1:9090/"),
        ("http://0177.0.0.1:9090", "http://127.0.0.1:9090/"),
        ("http://localhost:80/api/", "http://localhost:80/api/"),
        ("http://localhost/api", "http://localhost:80/api"),
        ("http://LOCALHOST:80/api", "http://localhost:80/api"),
        ("http://127.1:80/api", "http://127.0.0.1:80/api"),
        ("http://[::1]:80/api", "http://[::1]:80/api"),
        ("http://localhost:9090/api", "http://localhost:9090/api"),
        ("http://localhost:80/", "http://localhost/"),
        ("http://localhost/%2Fapi", "http://localhost:80/%2Fapi"),
        ("http://localhost?", "http://localhost:80/?"),
        ("http://localhost#", "http://localhost:80/#"),
        ("http://localhost:80/api?", "http://localhost:80/api?"),
        ("http://localhost:80/api#", "http://localhost:80/api#"),
    ],
)
def test_confirmed_local_endpoint_is_saved_in_runtime_loopback_form(
    tmp_path: Path, remembered: bool, supplied: str, normalized: str
) -> None:
    installer, env = fake_guide(tmp_path)
    env["CLAUDE_SMART_DEFER_SHARED_SERVICES"] = "1"
    selected = tmp_path / ".claude-smart/.env"
    arguments = ["--url", supplied]
    if remembered:
        selected.parent.mkdir()
        selected.write_text(
            'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="fake-key"\n'
            f'CLAUDE_SMART_LOCAL_URL="{supplied}"\n'
            'CLAUDE_SMART_LEARNING_PROVIDER="codex"\n'
        )
        arguments = []
    result = subprocess.run(
        [
            node_bin(),
            str(installer),
            "setup",
            "--host",
            "claude-code",
            "--backend",
            "local",
            "--provider",
            "codex",
            *arguments,
            "--yes",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        dotenv_values(stream=StringIO(selected.read_text()), interpolate=False).get(
            "REFLEXIO_URL"
        )
        == f"{normalized}"
    )
    assert (
        dotenv_values(stream=StringIO(selected.read_text()), interpolate=False).get(
            "REFLEXIO_API_KEY"
        )
        == ""
    )
    assert "Claude Code: installed" in result.stdout
    assert "incomplete or invalid" not in result.stderr
    # Public confirmation must agree with all three real runtime readers.
    from claude_smart.env_config import reflexio_url_is_remote

    assert not reflexio_url_is_remote(normalized)
    shell = subprocess.run(
        [
            "bash",
            "-c",
            (
                '. "$1"; if claude_smart_reflexio_url_is_remote; then exit 2; fi; '
                "REFLEXIO_URL=https://owned-remote.example/; "
                "claude_smart_reflexio_url_is_remote"
            ),
            "bash",
            str(LIB),
        ],
        env={**env, "REFLEXIO_URL": normalized},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert shell.returncode == 0, shell.stdout + shell.stderr


def test_installed_claude_slash_commands_follow_selector_file_without_junction(
    tmp_path,
):
    root = tmp_path / "active runtime"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("cli.sh", "dashboard-open.sh"):
        (scripts / name).write_text('printf "%s\n" "$0"')
    metadata = tmp_path / ".reflexio"
    metadata.mkdir()
    (metadata / "plugin-root.txt").write_text(str(root) + "\n")
    for command in (REPO_ROOT / "plugin/commands").glob("*.md"):
        invocation = next(
            line[2:-1]
            for line in command.read_text().splitlines()
            if line.startswith("!`")
        )
        env = dict(os.environ, HOME=str(tmp_path), ARGUMENTS="test")
        result = subprocess.run(
            ["bash", "-c", invocation],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (command.name, result.stderr)
        assert Path(result.stdout.strip()).parent == scripts


@pytest.mark.parametrize(
    "rows,expected",
    [
        # Confirmed against the actual 0c02dcba launcher: shell :-1 defaulting
        # precedes the Python SDK boolean read; whitespace is not empty.
        (
            "CLAUDE_SMART_LEARNING_PROVIDER=codex\nCLAUDE_SMART_LEARNING_MODEL=private-model\n",
            "local-cli/codex/private-model",
        ),
        (
            'CLAUDE_SMART_LEARNING_PROVIDER=codex\nCLAUDE_SMART_USE_LOCAL_CLI=""\nCLAUDE_SMART_LEARNING_MODEL=private-model\n',
            "local-cli/codex/private-model",
        ),
        (
            "CLAUDE_SMART_USE_LOCAL_CLI=1\nCLAUDE_SMART_LEARNING_MODEL=private-model\n",
            None,
        ),
        (
            'CLAUDE_SMART_USE_LOCAL_CLI=""\nCLAUDE_SMART_LEARNING_MODEL=private-model\n',
            None,
        ),
        (
            'CLAUDE_SMART_LEARNING_PROVIDER=codex\nCLAUDE_SMART_USE_LOCAL_CLI=" "\nCLAUDE_SMART_LEARNING_MODEL=private-model\n',
            "",
        ),
        (
            'CLAUDE_SMART_LEARNING_PROVIDER=" codex "\nCLAUDE_SMART_USE_LOCAL_CLI=1\nCLAUDE_SMART_LEARNING_MODEL=private-model\n',
            "local-cli/codex/private-model",
        ),
    ],
)
def test_legacy_selection_preserves_exact_boolean_and_host_semantics(
    tmp_path, rows, expected
):
    installer, env = fake_guide(tmp_path)
    env.pop("CLAUDE_SMART_HOST", None)
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = ("REFLEXIO_URL=http://localhost:8071/\n" + rows).encode()
    selected.write_bytes(original)
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
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if expected is None:
        assert result.returncode != 0
        assert "explicit --provider" in result.stderr
        assert selected.read_bytes() == original
        assert not (selected.parent / "native-selection-history").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert dotenv_values(selected)["REFLEXIO_NATIVE_CLI_MODEL"] == expected
        history = list((selected.parent / "native-selection-history").glob("*.env"))
        assert len(history) == 1 and history[0].read_bytes() == original


def test_first_local_activation_uses_explicit_provider_without_inventing_saved_host(
    tmp_path,
):
    installer, env = fake_guide(tmp_path)
    _copy_codex_install_sources(installer.parent.parent)
    _write_fake_codex(tmp_path / "fake-bin/codex")
    env.pop("CLAUDE_SMART_HOST", None)
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    selected.write_text(
        "REFLEXIO_URL=https://managed.example/\nREFLEXIO_API_KEY=fake-key\n"
    )
    command = [
        node_bin(),
        str(installer),
        "setup",
        "--host",
        "codex",
        "--backend",
        "local",
        "--yes",
    ]
    original = selected.read_bytes()
    refused = subprocess.run(
        command, env=env, text=True, capture_output=True, timeout=30, check=False
    )
    assert refused.returncode != 0
    assert "--provider" in refused.stderr
    assert selected.read_bytes() == original
    result = subprocess.run(
        [*command, "--provider", "codex"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert dotenv_values(selected)["REFLEXIO_NATIVE_CLI_MODEL"] == "local-cli/codex"


@pytest.mark.parametrize(
    "reference", ["not-native/codex/private-model", "local-cli/codex/"]
)
def test_saved_canonical_reference_is_not_repaired_into_an_unrequested_target(
    tmp_path, reference
):
    installer, env = fake_guide(tmp_path)
    env.pop("CLAUDE_SMART_HOST", None)
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = f"REFLEXIO_URL=http://localhost:8071/\nREFLEXIO_NATIVE_CLI_MODEL={reference}\n".encode()
    selected.write_bytes(original)
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
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert selected.read_bytes() == original
    assert not (selected.parent / "native-selection-history").exists()


@pytest.mark.parametrize(
    "references",
    [
        ["local-cli/codex/first-model", "local-cli/claude-code/sonnet"],
        ["", "local-cli/codex/second-model"],
        ["local-cli/codex/first-model", "local-cli/codex/second-model"],
    ],
)
@pytest.mark.parametrize("repair", ["provider", "caller"])
def test_unattended_keep_refuses_conflicting_native_assignments_then_explicit_choice_repairs(
    tmp_path, references, repair
):
    installer, env = fake_guide(tmp_path)
    _copy_codex_install_sources(installer.parent.parent)
    _write_fake_codex(tmp_path / "fake-bin/codex")
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    original = (
        "REFLEXIO_URL=http://localhost:8071/\nOTHER=keep\n"
        + "".join(
            f"REFLEXIO_NATIVE_CLI_MODEL={reference}\n" for reference in references
        )
    ).encode()
    selected.write_bytes(original)
    command = [
        node_bin(),
        str(installer),
        "setup",
        "--host",
        "claude-code",
        "--backend",
        "local",
        "--yes",
    ]
    refused = subprocess.run(
        command, env=env, text=True, capture_output=True, timeout=30, check=False
    )
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "ambiguous" in refused.stderr.lower()
    assert selected.read_bytes() == original
    assert not (selected.parent / "native-selection-history").exists()
    if repair == "provider":
        command.extend(["--provider", "codex"])
    else:
        env["REFLEXIO_NATIVE_CLI_MODEL"] = "local-cli/codex"
    confirmed = subprocess.run(
        command, env=env, text=True, capture_output=True, timeout=30, check=False
    )
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    assert dotenv_values(selected)["REFLEXIO_NATIVE_CLI_MODEL"] == "local-cli/codex"
    assert selected.read_text().count("REFLEXIO_NATIVE_CLI_MODEL=") == 1
    history = list((selected.parent / "native-selection-history").glob("*.env"))
    assert len(history) == 1 and history[0].read_bytes() == original


def test_unattended_keep_accepts_identical_native_assignments(tmp_path):
    installer, env = fake_guide(tmp_path)
    _copy_codex_install_sources(installer.parent.parent)
    _write_fake_codex(tmp_path / "fake-bin/codex")
    env.pop("REFLEXIO_NATIVE_CLI_MODEL", None)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    selected.write_text(
        "REFLEXIO_NATIVE_CLI_MODEL=local-cli/codex/kept-model\nREFLEXIO_NATIVE_CLI_MODEL=local-cli/codex/kept-model\n"
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
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        dotenv_values(selected)["REFLEXIO_NATIVE_CLI_MODEL"]
        == "local-cli/codex/kept-model"
    )
