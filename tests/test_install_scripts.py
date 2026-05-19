"""Focused checks for shell install helpers used by claude-smart setup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "plugin" / "scripts" / "_lib.sh"
SMART_INSTALL = REPO_ROOT / "plugin" / "scripts" / "smart-install.sh"
HOOK_ENTRY = REPO_ROOT / "plugin" / "scripts" / "hook_entry.sh"
CODEX_COMPAT = REPO_ROOT / "plugin" / "scripts" / "codex-claude-compat"
CODEX_HOOK = REPO_ROOT / "plugin" / "scripts" / "codex-hook.js"
BACKEND_LOG_RUNNER = REPO_ROOT / "plugin" / "scripts" / "backend-log-runner.sh"
NODE_INSTALLER = REPO_ROOT / "bin" / "claude-smart.js"


def _minimal_path(tmp_path: Path, *names: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in names:
        target = shutil.which(name)
        if target:
            (bin_dir / name).symlink_to(target)
    return str(bin_dir)


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["SHELL"] = "/bin/sh"
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    return env


def _node_platform() -> str:
    uname_s = subprocess.check_output(["uname", "-s"], text=True).strip()
    uname_m = subprocess.check_output(["uname", "-m"], text=True).strip()
    if uname_s.startswith("Darwin"):
        node_os = "darwin"
    elif uname_s.startswith("Linux"):
        node_os = "linux"
    else:
        pytest.skip(f"unsupported private Node test OS: {uname_s}")
    if uname_m in {"x86_64", "amd64"}:
        node_arch = "x64"
    elif uname_m in {"arm64", "aarch64"}:
        node_arch = "arm64"
    else:
        pytest.skip(f"unsupported private Node test arch: {uname_m}")
    return f"{node_os}-{node_arch}"


def _fake_node_dist(tmp_path: Path, *, bad_checksum: bool = False) -> Path:
    platform = _node_platform()
    dist = tmp_path / "nodejs-dist"
    source_root = tmp_path / f"node-v22.99.0-{platform}"
    bin_dir = source_root / "bin"
    bin_dir.mkdir(parents=True)
    for name, output in {"node": "v22.99.0", "npm": "10.9.0"}.items():
        executable = bin_dir / name
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    dist.mkdir()
    archive_name = f"{source_root.name}.tar.gz"
    archive_path = dist / archive_name
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source_root, arcname=source_root.name)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if bad_checksum:
        digest = "0" * 64
    (dist / "SHASUMS256.txt").write_text(f"{digest}  {archive_name}\n")
    return dist


def _run_private_node_install(tmp_path: Path, base_url: str) -> subprocess.CompletedProcess[str]:
    env = _isolated_env(tmp_path)
    env["CLAUDE_SMART_INSTALL_PRIVATE_NODE_ONLY"] = "1"
    env["CLAUDE_SMART_NODE_BASE_URL"] = base_url
    env["PATH"] = _minimal_path(
        tmp_path,
        "dirname",
        "mkdir",
        "rm",
        "uname",
        "mktemp",
        "awk",
        "sed",
        "tar",
        "ln",
        "mv",
        "cp",
        "sha256sum",
        "shasum",
        "openssl",
    )
    return subprocess.run(
        ["/bin/bash", str(SMART_INSTALL)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_dashboard_unavailable_marker_contains_recovery(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    script = (
        f'. "{LIB}"; '
        'claude_smart_write_dashboard_unavailable "simulated download failure"; '
        'cat "$HOME/.claude-smart/dashboard-unavailable"'
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "simulated download failure" in result.stdout
    assert "The learning backend and hooks can still work" in result.stdout
    assert "/claude-smart:restart" in result.stdout
    assert str(tmp_path / ".claude-smart" / "node" / "current") in result.stdout


def test_trim_log_file_preserves_newest_bytes(tmp_path: Path) -> None:
    log = tmp_path / ".claude-smart" / "backend.log"
    log.parent.mkdir()
    log.write_text("0123456789abcde")
    env = _isolated_env(tmp_path)

    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            f'. "{LIB}"; claude_smart_trim_log_file "{log}" 10',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text() == "56789abcde"
    assert log.stat().st_size == 10


def test_append_capped_log_keeps_file_under_limit(tmp_path: Path) -> None:
    log = tmp_path / ".claude-smart" / "backend.log"
    log.parent.mkdir()
    log.write_text("0123456789")
    env = _isolated_env(tmp_path)

    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            f'. "{LIB}"; claude_smart_append_capped_log "{log}" 12 "abcdef"',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text() == "56789abcdef\n"
    assert log.stat().st_size == 12


def test_backend_log_runner_caps_streamed_output(tmp_path: Path) -> None:
    log = tmp_path / ".claude-smart" / "backend.log"
    env = _isolated_env(tmp_path)

    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            str(BACKEND_LOG_RUNNER),
            str(log),
            "120",
            "--",
            "/bin/bash",
            "-c",
            (
                "for i in 1 2 3 4 5 6 7 8 9 10; do "
                "printf 'stdout-%02d-xxxxxxxxxx\\n' \"$i\"; "
                "printf 'stderr-%02d-yyyyyyyyyy\\n' \"$i\" >&2; "
                "done"
            ),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = log.read_text()
    assert log.stat().st_size <= 120
    assert "stderr-10-yyyyyyyyyy" in content
    assert "stdout-01-xxxxxxxxxx" not in content


def test_backend_service_uses_capped_logging() -> None:
    service = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert "backend-log-runner.sh" in service
    assert "claude_smart_append_capped_log" in service
    assert '>>"$LOG_FILE" 2>&1' not in service
    assert '>>"$LOG_FILE"' not in service


def test_service_start_scripts_recover_missing_dependencies_without_cli_command() -> None:
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()
    dashboard = (REPO_ROOT / "plugin" / "scripts" / "dashboard-service.sh").read_text()

    assert "[claude-smart] backend: uv not on PATH; running installer" in backend
    assert "CLAUDE_SMART_BOOTSTRAPPING=1 bash \"$PLUGIN_ROOT/scripts/smart-install.sh\"" in backend
    assert "[claude-smart] backend: uv not on PATH after installer; skipping" in backend
    assert "[claude-smart] dashboard: npm is not on PATH; running installer" in dashboard
    assert "CLAUDE_SMART_BOOTSTRAPPING=1 bash \"$PLUGIN_ROOT/scripts/smart-install.sh\"" in dashboard
    assert "npm is not on PATH after installer; dashboard cannot start" in dashboard


def test_service_start_scripts_guard_internal_invocations() -> None:
    lib = (REPO_ROOT / "plugin" / "scripts" / "_lib.sh").read_text()
    hook_entry = HOOK_ENTRY.read_text()
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()
    dashboard = (REPO_ROOT / "plugin" / "scripts" / "dashboard-service.sh").read_text()

    assert "claude_smart_is_internal_invocation_env()" in lib
    assert "CLAUDE_CODE_ENTRYPOINT" in lib
    assert "if claude_smart_is_internal_invocation_env; then" in hook_entry
    assert "if claude_smart_is_internal_invocation_env; then" in backend
    assert "if claude_smart_is_internal_invocation_env; then" in dashboard


def test_backend_service_configures_shared_embedding_daemon() -> None:
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'EMBEDDING_PORT="${EMBEDDING_PORT:-8072}"' in backend
    assert 'REFLEXIO_EMBEDDING_PROVIDER:-local_service' in backend
    assert "REFLEXIO_EMBEDDING_SERVICE_URL" in backend


def test_codex_hook_recovers_missing_dependencies_without_cli_command() -> None:
    script = CODEX_HOOK.read_text()

    assert "function runInstaller(root, reason)" in script
    assert "function startInstallerDetached(root, reason)" in script
    assert 'runInstaller(root, "backend: uv not on PATH")' in script
    assert 'runInstaller(root, "dashboard: npm not on PATH")' in script
    assert 'runInstaller(root, "hook: uv not on PATH")' in script


def test_smart_install_waits_on_existing_lock() -> None:
    script = SMART_INSTALL.read_text()

    assert "flock -n" not in script
    assert "flock 9" in script
    assert 'INSTALL_REAP_LOCK="$MARKER_DIR/install.lock.reap"' in script
    assert 'mkdir "$INSTALL_REAP_LOCK"' in script
    assert "set -C" in script
    assert 'remove_stale_install_lock "$lock_pid"' in script
    assert '[ "$(cat "$INSTALL_LOCK" 2>/dev/null || true)" = "$$" ]' in script
    assert "kill -0 \"$lock_pid\"" in script


def test_smart_install_waits_on_portable_lock_without_flock(tmp_path: Path) -> None:
    bin_dir = Path(_minimal_path(tmp_path, "dirname", "mkdir", "rm", "cat", "sleep", "sed", "awk"))
    for name, output in {"node": "v22.99.0", "npm": "10.9.0"}.items():
        executable = bin_dir / name
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["CLAUDE_SMART_INSTALL_PRIVATE_NODE_ONLY"] = "1"
    env["PATH"] = str(bin_dir)
    env["SHELL"] = ""

    state_dir = tmp_path / ".claude-smart"
    state_dir.mkdir()
    lock = state_dir / "install.lock"
    locker = subprocess.Popen(
        ["/bin/sh", "-c", f'printf "%s\\n" "$$" > "{lock}"; sleep 1; rm -f "{lock}"'],
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not lock.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lock.exists()

        started = time.monotonic()
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", str(SMART_INSTALL)],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        elapsed = time.monotonic() - started
    finally:
        locker.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert elapsed >= 0.8
    assert not lock.exists()


def test_smart_install_reaps_stale_portable_lock_without_flock(tmp_path: Path) -> None:
    bin_dir = Path(_minimal_path(tmp_path, "dirname", "mkdir", "rm", "rmdir", "cat", "sleep", "sed", "awk"))
    for name, output in {"node": "v22.99.0", "npm": "10.9.0"}.items():
        executable = bin_dir / name
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["CLAUDE_SMART_INSTALL_PRIVATE_NODE_ONLY"] = "1"
    env["PATH"] = str(bin_dir)
    env["SHELL"] = ""

    state_dir = tmp_path / ".claude-smart"
    state_dir.mkdir()
    lock = state_dir / "install.lock"
    lock.write_text("999999999\n")

    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", str(SMART_INSTALL)],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not lock.exists()
    assert not (state_dir / "install.lock.reap").exists()


def test_claude_code_install_hook_matches_session_start_modes() -> None:
    hooks = json.loads((REPO_ROOT / "plugin" / "hooks" / "hooks.json").read_text())
    install_block = hooks["hooks"]["SessionStart"][0]
    runtime_block = hooks["hooks"]["SessionStart"][1]

    assert install_block["matcher"] == runtime_block["matcher"]
    assert install_block["matcher"] == "startup|clear|compact|resume"
    assert "smart-install.sh" in install_block["hooks"][0]["command"]
    session_start_hook = runtime_block["hooks"][1]
    assert "hook_entry.sh" in session_start_hook["command"]
    assert session_start_hook["timeout"] == 300
    backend_hook = runtime_block["hooks"][2]
    dashboard_hook = runtime_block["hooks"][3]
    assert "backend-service.sh" in backend_hook["command"]
    assert backend_hook["timeout"] == 300
    assert "dashboard-service.sh" in dashboard_hook["command"]
    assert dashboard_hook["timeout"] == 300


def test_codex_session_start_hook_has_install_recovery_budget() -> None:
    hooks = json.loads((REPO_ROOT / "plugin" / "hooks" / "codex-hooks.json").read_text())
    session_hooks = hooks["hooks"]["SessionStart"][0]["hooks"]
    hook_entry = next(hook for hook in session_hooks if "hook_entry.sh" in hook["command"])
    backend_hook = next(hook for hook in session_hooks if "backend-service.sh" in hook["command"])
    dashboard_hook = next(hook for hook in session_hooks if "dashboard-service.sh" in hook["command"])

    assert hook_entry["timeout"] == 300
    assert backend_hook["timeout"] == 300
    assert dashboard_hook["timeout"] == 300


def test_hook_entry_self_heals_missing_uv_without_cli_command() -> None:
    script = HOOK_ENTRY.read_text()

    assert "CLAUDE_SMART_BOOTSTRAPPING=1 bash \"$PLUGIN_ROOT/scripts/smart-install.sh\"" in script
    assert "claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1" in script
    assert 'bash "$HERE/backend-service.sh" start' in script
    assert 'bash "$HERE/dashboard-service.sh" start' in script


def test_node_installer_platform_preflight_messages() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for installer wrapper tests")

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        "console.log(installer.platformSupportError() || 'ok');"
    )
    cases = [
        ({"CLAUDE_SMART_TEST_PLATFORM": "darwin", "CLAUDE_SMART_TEST_ARCH": "arm64", "CLAUDE_SMART_TEST_RELEASE": "23.0.0"}, "ok"),
        ({"CLAUDE_SMART_TEST_PLATFORM": "darwin", "CLAUDE_SMART_TEST_ARCH": "x64", "CLAUDE_SMART_TEST_RELEASE": "23.0.0"}, "Intel Mac is not supported"),
        ({"CLAUDE_SMART_TEST_PLATFORM": "darwin", "CLAUDE_SMART_TEST_ARCH": "arm64", "CLAUDE_SMART_TEST_RELEASE": "22.6.0"}, "macOS 13 and older are not supported"),
        ({"CLAUDE_SMART_TEST_PLATFORM": "win32", "CLAUDE_SMART_TEST_ARCH": "arm64"}, "Windows ARM is not supported"),
        ({"CLAUDE_SMART_TEST_PLATFORM": "win32", "CLAUDE_SMART_TEST_ARCH": "x64"}, "ok"),
    ]
    for overrides, expected in cases:
        env = os.environ.copy()
        env.update(overrides)
        result = subprocess.run(
            [node, "-e", script],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert expected in result.stdout.strip()


def test_node_installer_does_not_treat_global_node_as_private_runtime(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for installer wrapper tests")

    home = tmp_path / "home"
    global_bin = tmp_path / "global-bin"
    global_bin.mkdir(parents=True)
    for name in ("node", "npm"):
        executable = global_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        "installer.ensurePrivateNode()"
        ".then(() => { console.error('unexpected success'); process.exit(1); })"
        ".catch((err) => { console.log(err.message); });"
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": str(global_bin),
            "CLAUDE_SMART_TEST_PLATFORM": "darwin",
            "CLAUDE_SMART_TEST_ARCH": "x64",
            "CLAUDE_SMART_TEST_RELEASE": "23.0.0",
        }
    )
    result = subprocess.run(
        [node, "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Intel Mac is not supported" in result.stdout


def test_node_installer_bootstraps_runtime_with_private_node_and_uv(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for installer wrapper tests")

    home = tmp_path / "home"
    plugin_root = tmp_path / "plugin"
    dashboard = plugin_root / "dashboard"
    hooks = plugin_root / "hooks"
    private_node = home / ".claude-smart" / "node" / "current" / "bin"
    uv_bin = home / ".local" / "bin"
    dashboard.mkdir(parents=True)
    hooks.mkdir(parents=True)
    private_node.mkdir(parents=True)
    uv_bin.mkdir(parents=True)
    (plugin_root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    # Use realistic command fixtures so the content-based patcher in
    # patchCodexHooksForNode can dispatch by script name. The install hook
    # at index 0 must survive patching because it has to run as bash.
    (hooks / "codex-hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {"command": 'bash "$_R/scripts/smart-install.sh"'},
                                {"command": 'bash "$_R/scripts/ensure-plugin-root.sh" "$_R"'},
                                {"command": 'bash "$_R/scripts/backend-service.sh" start'},
                                {"command": 'bash "$_R/scripts/dashboard-service.sh" start'},
                                {"command": 'bash "$_R/scripts/hook_entry.sh" codex session-start'},
                            ]
                        }
                    ],
                    "UserPromptSubmit": [{"hooks": [{"command": 'bash "$_R/scripts/hook_entry.sh" codex user-prompt'}]}],
                    "PostToolUse": [{"hooks": [{"command": 'bash "$_R/scripts/hook_entry.sh" codex post-tool'}]}],
                    "Stop": [{"hooks": [{"command": 'bash "$_R/scripts/hook_entry.sh" codex stop'}]}],
                }
            }
        )
        + "\n"
    )
    (private_node / "node").write_text("#!/bin/sh\nexit 0\n")
    (private_node / "npm").write_text(
        "#!/bin/sh\nprintf 'npm %s\\n' \"$*\" >> \"$HOME/npm.log\"\nexit 0\n"
    )
    (uv_bin / "uv").write_text(
        "#!/bin/sh\nprintf 'uv %s\\n' \"$*\" >> \"$HOME/uv.log\"\nexit 0\n"
    )
    for executable in [private_node / "node", private_node / "npm", uv_bin / "uv"]:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        f"installer.bootstrapPluginRuntime({json.dumps(str(plugin_root))})"
        ".then(() => console.log('done')).catch((err) => { console.error(err.message); process.exit(1); });"
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CLAUDE_SMART_TEST_PLATFORM": "darwin",
            "CLAUDE_SMART_TEST_ARCH": "arm64",
            "CLAUDE_SMART_TEST_RELEASE": "23.0.0",
        }
    )
    result = subprocess.run(
        [node, "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "done" in result.stdout
    assert "uv sync --locked --python 3.12 --quiet" in (home / "uv.log").read_text()
    npm_log = (home / "npm.log").read_text()
    assert "npm ci" in npm_log
    assert "npm run build" in npm_log
    parsed = json.loads((hooks / "codex-hooks.json").read_text())
    session_hooks = parsed["hooks"]["SessionStart"][0]["hooks"]
    # Index 0 is smart-install.sh — must run as bash, not through node.
    install_cmd = session_hooks[0]["command"]
    assert "smart-install.sh" in install_cmd
    assert "codex-hook.js" not in install_cmd
    # Indices 1-4 dispatch to node by command content. Verify each maps to
    # the right codex-hook.js subcommand and references the private node.
    # Each arg is independently shell-quoted, so the sub-event appears as
    # `"hook" "session-start"` rather than `hook session-start`.
    expected_subs = [["ensure-root"], ["backend"], ["dashboard"], ["hook", "session-start"]]
    for idx, sub_tokens in enumerate(expected_subs, start=1):
        cmd = session_hooks[idx]["command"]
        assert str(private_node / "node") in cmd, f"index {idx}: {cmd}"
        assert "codex-hook.js" in cmd, f"index {idx}: {cmd}"
        for token in sub_tokens:
            assert f'"{token}"' in cmd, f"index {idx} expected token {token!r}: {cmd}"
    user_prompt_cmd = parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert '"hook"' in user_prompt_cmd and '"user-prompt"' in user_prompt_cmd


def test_resolve_npm_accepts_windows_cmd_shim(tmp_path: Path) -> None:
    npm_cmd = tmp_path / "npm.cmd"
    npm_cmd.write_text("#!/bin/sh\nprintf '10.0.0\\n'\n")
    npm_cmd.chmod(npm_cmd.stat().st_mode | stat.S_IXUSR)
    env = _isolated_env(tmp_path)
    env["PATH"] = str(tmp_path)
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            f'. "{LIB}"; claude_smart_resolve_npm; claude_smart_npm_available',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[0] == str(npm_cmd)


def test_dashboard_build_writes_marker_when_npm_missing(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    env["PATH"] = _minimal_path(tmp_path, "dirname", "mkdir", "cat", "uname", "rm")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            str(REPO_ROOT / "plugin" / "scripts" / "dashboard-build.sh"),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert result.returncode == 1
    assert marker.is_file()
    assert "npm is not on PATH" in marker.read_text()


def test_codex_claude_compat_translates_claude_contract(tmp_path: Path) -> None:
    output_file = tmp_path / "captured-output-path"
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"--output-last-message\" ]; then\n"
        "    shift\n"
        "    out=\"$1\"\n"
        "  fi\n"
        "  shift || exit 1\n"
        "done\n"
        "cat > \"$out.prompt\"\n"
        "printf 'codex reply' > \"$out\"\n"
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    env = _isolated_env(tmp_path)
    env["PATH"] = _minimal_path(tmp_path, "cat", "node", "sh")
    env["CLAUDE_SMART_CODEX_PATH"] = str(codex)
    env["TMPDIR"] = str(tmp_path)

    result = subprocess.run(
        [
            str(CODEX_COMPAT),
            "-p",
            "--output-format",
            "json",
            "--model",
            "claude-sonnet-4-6",
            "--append-system-prompt",
            "system rules",
        ],
        input="answer this",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"result": "codex reply"}
    prompt_files = list(tmp_path.glob("claude-smart-codex-*.prompt"))
    assert len(prompt_files) == 1
    assert prompt_files[0].read_text() == "system rules\n\n## Task\nanswer this"
    assert not output_file.exists()


def test_codex_claude_compat_accepts_stream_json_flags(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"--output-last-message\" ]; then\n"
        "    shift\n"
        "    out=\"$1\"\n"
        "  fi\n"
        "  shift || exit 1\n"
        "done\n"
        "printf 'stream reply' > \"$out\"\n"
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    env = _isolated_env(tmp_path)
    env["PATH"] = _minimal_path(tmp_path, "node", "sh")
    env["CLAUDE_SMART_CODEX_PATH"] = str(codex)
    env["TMPDIR"] = str(tmp_path)

    result = subprocess.run(
        [
            str(CODEX_COMPAT),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            "claude-sonnet-4-6",
        ],
        input="answer this",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "result",
        "subtype": "success",
        "result": "stream reply",
    }


def test_codex_hook_ensure_root_tracks_active_plugin_root(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    local_root = tmp_path / "repo" / "plugin"
    cache_root = (
        tmp_path
        / ".codex"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / "0.2.26"
    )
    local_root.mkdir(parents=True)
    cache_root.mkdir(parents=True)
    (local_root / "pyproject.toml").write_text("[project]\nname='local'\n")
    (cache_root / "pyproject.toml").write_text("[project]\nname='cache'\n")
    reflexio = tmp_path / ".reflexio"
    reflexio.mkdir()
    (reflexio / "plugin-root").symlink_to(local_root, target_is_directory=True)

    env = _isolated_env(tmp_path)
    env["CLAUDE_PLUGIN_ROOT"] = str(cache_root)
    result = subprocess.run(
        [node, str(CODEX_HOOK), "ensure-root"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (reflexio / "plugin-root").resolve() == cache_root
    assert json.loads(result.stdout) == {"continue": True, "suppressOutput": True}


def test_codex_hook_caps_backend_log_appends(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    state_dir = tmp_path / ".claude-smart"
    state_dir.mkdir()
    log = state_dir / "backend.log"
    log.write_text("0" * (10_000_000 + 200))

    env = _isolated_env(tmp_path)
    env["PATH"] = ""
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT / "plugin")
    result = subprocess.run(
        [node, str(CODEX_HOOK), "backend"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"continue": True, "suppressOutput": True}
    assert log.stat().st_size <= 10_000_000


def test_install_fingerprint_hash_tracks_lib_changes(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    scripts = plugin_root / "scripts"
    dashboard = plugin_root / "dashboard"
    scripts.mkdir(parents=True)
    dashboard.mkdir()
    (scripts / "smart-install.sh").write_text("#!/bin/sh\n")
    (scripts / "_lib.sh").write_text("helper=v1\n")
    (plugin_root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    (plugin_root / "uv.lock").write_text("version = 1\n")
    (dashboard / "package.json").write_text("{}\n")
    (dashboard / "package-lock.json").write_text("{}\n")

    env = _isolated_env(tmp_path)
    env["PATH"] = _minimal_path(
        tmp_path,
        "awk",
        "cksum",
        "mktemp",
        "rm",
        "sha256sum",
        "shasum",
        "openssl",
    )
    command = (
        f'. "{LIB}"; '
        f'claude_smart_install_fingerprint_hash "{plugin_root}" "{scripts}"'
    )
    before = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    (scripts / "_lib.sh").write_text("helper=v2\n")
    after = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert before.returncode == 0, before.stderr
    assert after.returncode == 0, after.stderr
    assert before.stdout.strip()
    assert before.stdout != after.stdout


def test_install_private_node_installs_from_verified_archive(tmp_path: Path) -> None:
    dist = _fake_node_dist(tmp_path)
    result = _run_private_node_install(tmp_path, f"file://{dist}")

    assert result.returncode == 0, result.stderr
    current = tmp_path / ".claude-smart" / "node" / "current"
    assert current.exists()
    assert (current / "bin" / "node").exists()
    assert not (tmp_path / ".claude-smart" / "dashboard-unavailable").exists()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()


def test_install_private_node_checksum_failure_is_dashboard_only(tmp_path: Path) -> None:
    dist = _fake_node_dist(tmp_path, bad_checksum=True)
    result = _run_private_node_install(tmp_path, f"file://{dist}")

    assert result.returncode == 1
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert marker.is_file()
    assert "checksum verification failed" in marker.read_text()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()


def test_install_private_node_download_failure_is_dashboard_only(tmp_path: Path) -> None:
    missing_dist = tmp_path / "missing-node-dist"
    result = _run_private_node_install(tmp_path, f"file://{missing_dist}")

    assert result.returncode == 1
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert marker.is_file()
    assert "could not download Node.js checksums" in marker.read_text()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()
