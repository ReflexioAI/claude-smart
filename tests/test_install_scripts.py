"""Focused checks for shell install helpers used by claude-smart setup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "plugin" / "scripts" / "_lib.sh"
SMART_INSTALL = REPO_ROOT / "plugin" / "scripts" / "smart-install.sh"
SYNC_REFLEXIO_DEP = REPO_ROOT / "scripts" / "sync-reflexio-dep.py"
CLAUDE_SMART_BIN = REPO_ROOT / "bin" / "claude-smart.js"
CHECK_REFLEXIO_LOCK = REPO_ROOT / "scripts" / "check-reflexio-lock.py"
VENDOR_REFLEXIO = REPO_ROOT / "scripts" / "vendor-reflexio.py"
RELEASE_WITH_REFLEXIO = REPO_ROOT / "scripts" / "release-with-reflexio.sh"
SETUP_CLAUDE_SMART = REPO_ROOT / "scripts" / "setup-claude-smart.sh"
HOOK_ENTRY = REPO_ROOT / "plugin" / "scripts" / "hook_entry.sh"
CLI_SH = REPO_ROOT / "plugin" / "scripts" / "cli.sh"
CODEX_COMPAT = REPO_ROOT / "plugin" / "scripts" / "codex-claude-compat"
CODEX_HOOK = REPO_ROOT / "plugin" / "scripts" / "codex-hook.js"
BACKEND_LOG_RUNNER = REPO_ROOT / "plugin" / "scripts" / "backend-log-runner.sh"
NODE_INSTALLER = REPO_ROOT / "bin" / "claude-smart.js"
HEALTH_ROUTE = (
    REPO_ROOT / "plugin" / "dashboard" / "app" / "api" / "health" / "route.ts"
)


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


def _claude_plugin_cache_version_dir(tmp_path: Path) -> Path:
    """Return the cache dir that real `claude plugin install` would populate.

    bin/claude-smart.js no longer falls back to PACKAGE_ROOT/plugin when this
    dir is missing (npx scratch is too fragile), so installer tests that drive
    a fake `claude` binary must seed the cache with a real pyproject.toml +
    scripts/smart-install.sh so findClaudeCodePluginRoot can locate it.
    """
    pyproject = (REPO_ROOT / "plugin" / "pyproject.toml").read_text()
    version_line = next(
        (
            line
            for line in pyproject.splitlines()
            if line.strip().startswith("version =")
        ),
        "",
    )
    version = version_line.split("=", 1)[1].strip().strip('"') or "0.0.0"
    return (
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / version
    )


def _seed_fake_claude_cache(tmp_path: Path) -> Path:
    """Pre-populate the cache dir that fake `claude plugin install` won't create.

    Copies the real plugin tree (sans node_modules / .venv / caches) so the
    installer can run smart-install.sh against it. Returns the seeded path.
    """
    cache_dir = _claude_plugin_cache_version_dir(tmp_path)
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    src = REPO_ROOT / "plugin"

    def ignore(_: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name
            in {
                "__pycache__",
                ".venv",
                ".pytest_cache",
                ".ruff_cache",
                "node_modules",
                ".next",
            }
        }

    shutil.copytree(src, cache_dir, ignore=ignore, dirs_exist_ok=False)
    return cache_dir


def _fake_claude_install_script() -> str:
    """Default fake `claude` body that logs every call and exits 0.

    Used by tests that don't care about install-retry semantics. The cache is
    seeded separately via _seed_fake_claude_cache, so this script just needs
    to not error.
    """
    return '#!/bin/sh\nprintf \'claude %s\\n\' "$*" >> "$HOME/claude.log"\nexit 0\n'


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


def _run_private_node_install(
    tmp_path: Path, base_url: str
) -> subprocess.CompletedProcess[str]:
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


def test_backend_service_forces_utf8_stdio_for_managed_backend() -> None:
    service = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'env PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"' in service


def test_backend_service_pins_reflexio_home_to_user_home() -> None:
    service = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'export REFLEXIO_LOG_DIR="${REFLEXIO_LOG_DIR:-$HOME}"' in service


def test_service_start_scripts_recover_missing_dependencies_without_cli_command() -> (
    None
):
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()
    dashboard = (REPO_ROOT / "plugin" / "scripts" / "dashboard-service.sh").read_text()

    assert (
        "[claude-smart] backend: uv not on PATH; starting installer in background"
        in backend
    )
    assert "claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1" in backend
    assert (
        "[claude-smart] backend: uv not on PATH; installer recovery scheduled; skipping"
        in backend
    )
    assert (
        "[claude-smart] dashboard: npm is not on PATH; starting installer in background"
        in dashboard
    )
    assert "claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1" in dashboard
    assert (
        "npm is not on PATH; installer recovery scheduled; dashboard cannot start yet"
        in dashboard
    )


def test_backend_service_skips_local_start_for_remote_reflexio_url() -> None:
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()
    lib = (REPO_ROOT / "plugin" / "scripts" / "_lib.sh").read_text()

    assert "claude_smart_source_reflexio_env" in backend
    assert "claude_smart_reflexio_url_is_remote()" in lib
    assert "remote REFLEXIO_URL configured; skipping local backend start" in backend
    assert "remote configured at $REFLEXIO_URL" in backend


def test_smart_install_repairs_local_env_defaults() -> None:
    script = (REPO_ROOT / "plugin" / "scripts" / "smart-install.sh").read_text()
    lib = (REPO_ROOT / "plugin" / "scripts" / "_lib.sh").read_text()

    assert 'touch "$REFLEXIO_ENV"' in script
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in script
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in script
    assert "CLAUDE_SMART_READ_ONLY=0" in script
    assert "CLAUDE_SMART_USE_LOCAL_CLI=" in script.split("install_complete()", 1)[1]
    assert "claude_smart_source_reflexio_env" in script
    assert "CLAUDE_SMART_READ_ONLY" in lib
    assert "REFLEXIO_USER_ID" in lib


def test_reflexio_env_file_overrides_stale_managed_process_env(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="rflx-file-secret"\n'
    )

    script = (
        f'. "{LIB}"; '
        "claude_smart_source_reflexio_env; "
        'printf "%s\\n%s\\n" "$REFLEXIO_URL" "$REFLEXIO_API_KEY"'
    )
    env = _isolated_env(tmp_path)
    env["REFLEXIO_URL"] = "https://stale.example/"
    env["REFLEXIO_API_KEY"] = "rflx-stale-secret"

    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://www.reflexio.ai/",
        "rflx-file-secret",
    ]


def test_reflexio_env_loader_exports_read_only_flag(tmp_path: Path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text('CLAUDE_SMART_READ_ONLY="1"\n')

    script = (
        f'. "{LIB}"; '
        "claude_smart_source_reflexio_env; "
        'printf "%s\\n" "${CLAUDE_SMART_READ_ONLY:-}"'
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        env=_isolated_env(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def _run_setup_script(tmp_path: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    env = _isolated_env(tmp_path)
    env["CLAUDE_SMART_SETUP_NO_INSTALL"] = "1"
    env["REFLEXIO_ENV_PATH"] = str(tmp_path / ".reflexio" / ".env")
    return subprocess.run(
        ["/bin/bash", str(SETUP_CLAUDE_SMART)],
        input=stdin,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_script_local_mode_creates_env(tmp_path: Path) -> None:
    result = _run_setup_script(tmp_path, "claude-code\nlocal\n")

    assert result.returncode == 0, result.stderr
    text = (tmp_path / ".reflexio" / ".env").read_text()
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in text
    assert 'CLAUDE_SMART_READ_ONLY="0"' in text


def test_setup_script_local_mode_cleans_managed_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        "# keep\nUNKNOWN=value\nREFLEXIO_URL=x\nREFLEXIO_API_KEY=y\n"
        "REFLEXIO_USER_ID=z\nCLAUDE_SMART_READ_ONLY=1\n"
    )

    result = _run_setup_script(tmp_path, "claude-code\nlocal\n")

    assert result.returncode == 0, result.stderr
    text = env_path.read_text()
    assert "# keep" in text
    assert "UNKNOWN=value" in text
    assert "REFLEXIO_URL" not in text
    assert "REFLEXIO_API_KEY" not in text
    assert "REFLEXIO_USER_ID" not in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in text
    assert "CLAUDE_SMART_READ_ONLY=1" in text
    assert 'CLAUDE_SMART_READ_ONLY="0"' not in text


def test_setup_script_local_mode_replaces_managed_only_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text('REFLEXIO_API_KEY="rflx-test"\n')

    result = _run_setup_script(tmp_path, "claude-code\nlocal\n")

    assert result.returncode == 0, result.stderr
    text = env_path.read_text()
    assert "REFLEXIO_API_KEY" not in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in text
    assert 'CLAUDE_SMART_READ_ONLY="0"' in text


def test_setup_script_managed_project_scoped(tmp_path: Path) -> None:
    result = _run_setup_script(
        tmp_path,
        "claude-code\nmanaged\nrflx-test-secret\nno\nproject\n",
    )

    assert result.returncode == 0, result.stderr
    text = (tmp_path / ".reflexio" / ".env").read_text()
    assert 'REFLEXIO_URL="https://www.reflexio.ai/"' in text
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in text
    assert "REFLEXIO_USER_ID" not in text
    assert "CLAUDE_SMART_READ_ONLY" not in text
    assert "rotate" in result.stderr


def test_setup_script_managed_rewrites_existing_managed_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        "# keep\n"
        "UNKNOWN=value\n"
        'REFLEXIO_URL="https://old.example/"\n'
        'REFLEXIO_API_KEY="rflx-old-one"\n'
        'REFLEXIO_API_KEY="rflx-old-two"\n'
        'CLAUDE_SMART_READ_ONLY="1"\n'
        'REFLEXIO_USER_ID="old-user"\n'
    )

    result = _run_setup_script(
        tmp_path,
        "claude-code\nmanaged\nrflx-new-secret\nno\nproject\n",
    )

    assert result.returncode == 0, result.stderr
    text = env_path.read_text()
    assert "# keep" in text
    assert "UNKNOWN=value" in text
    assert text.count("REFLEXIO_URL=") == 1
    assert text.count("REFLEXIO_API_KEY=") == 1
    assert 'REFLEXIO_URL="https://old.example/"' in text
    assert 'REFLEXIO_API_KEY="rflx-new-secret"' in text
    assert "rflx-old" not in text
    assert "CLAUDE_SMART_READ_ONLY" not in text
    assert "REFLEXIO_USER_ID" not in text


def test_setup_script_managed_removes_local_mode_variables(tmp_path: Path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        "# keep\n"
        "UNKNOWN=value\n"
        'REFLEXIO_URL="http://localhost:8071/"\n'
        "CLAUDE_SMART_USE_LOCAL_CLI=1\n"
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1\n"
    )

    result = _run_setup_script(
        tmp_path,
        "claude-code\nmanaged\nrflx-new-secret\nno\nproject\n",
    )

    assert result.returncode == 0, result.stderr
    text = env_path.read_text()
    assert "# keep" in text
    assert "UNKNOWN=value" in text
    assert 'REFLEXIO_URL="https://www.reflexio.ai/"' in text
    assert 'REFLEXIO_API_KEY="rflx-new-secret"' in text
    assert "localhost" not in text
    assert "CLAUDE_SMART_USE_LOCAL_CLI" not in text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING" not in text


def test_setup_script_managed_global_read_only(tmp_path: Path) -> None:
    result = _run_setup_script(
        tmp_path,
        "codex\nmanaged\nrflx-test-secret\nyes\nglobal\n",
    )

    assert result.returncode == 0, result.stderr
    text = (tmp_path / ".reflexio" / ".env").read_text()
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in text
    assert 'CLAUDE_SMART_READ_ONLY="1"' in text
    assert 'REFLEXIO_USER_ID="global_user"' in text


def test_setup_script_rerun_keeps_existing_api_key_on_enter(tmp_path: Path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\n'
        'REFLEXIO_API_KEY="rflx-existing-secret"\n'
        'REFLEXIO_USER_ID="global_user"\n'
    )

    result = _run_setup_script(tmp_path, "\n\n\n\n\n")

    assert result.returncode == 0, result.stderr
    text = env_path.read_text()
    assert 'REFLEXIO_API_KEY="rflx-existing-secret"' in text
    assert 'REFLEXIO_USER_ID="global_user"' in text
    assert "****cret" in result.stderr


def test_setup_script_rerun_preserves_custom_global_user_id(tmp_path: Path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\n'
        'REFLEXIO_API_KEY="rflx-existing-secret"\n'
        'REFLEXIO_USER_ID="custom-user"\n'
    )

    result = _run_setup_script(tmp_path, "\n\n\n\n\n")

    assert result.returncode == 0, result.stderr
    assert 'REFLEXIO_USER_ID="custom-user"' in env_path.read_text()


def test_setup_script_rejects_whitespace_api_key(tmp_path: Path) -> None:
    result = _run_setup_script(
        tmp_path,
        "claude-code\nmanaged\nbad key\nrflx-good-key\nno\nproject\n",
    )

    assert result.returncode == 0, result.stderr
    assert "must not contain whitespace" in result.stderr
    assert (
        'REFLEXIO_API_KEY="rflx-good-key"'
        in (tmp_path / ".reflexio" / ".env").read_text()
    )


def test_node_installer_ignores_stale_url_without_api_key(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Node installer test")

    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text('REFLEXIO_URL="https://managed.example/"\n')
    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        "installer.configureReflexioSetup();"
        "process.stdout.write(process.env.REFLEXIO_URL || '');"
    )
    env = _isolated_env(tmp_path)
    env["REFLEXIO_URL"] = "https://stale.example/"

    result = subprocess.run(
        [node, "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith(
        "CLAUDE_SMART_USE_LOCAL_CLI, "
        "CLAUDE_SMART_USE_LOCAL_EMBEDDING, CLAUDE_SMART_READ_ONLY.\n"
    )
    env_text = env_path.read_text()
    assert "REFLEXIO_URL" not in env_text
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in env_text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in env_text
    assert 'CLAUDE_SMART_READ_ONLY="0"' in env_text


def test_node_installer_supports_managed_reflexio_setup() -> None:
    installer = NODE_INSTALLER.read_text()

    assert 'const MANAGED_REFLEXIO_URL = "https://www.reflexio.ai/";' in installer
    assert 'cmd === "setup"' in installer
    assert "setup-claude-smart.sh" in installer
    assert 'parseOptionalArg(args, "--api-key")' not in installer
    assert '"--read-only"' not in installer
    assert "REFLEXIO_API_KEY" in installer
    assert "CLAUDE_SMART_MANAGED_SETUP" in installer
    assert "configureReflexioSetup()" in installer
    assert "maskSecret(apiKey)" in installer
    script = SMART_INSTALL.read_text()
    assert "configured managed Reflexio" not in script


def test_node_installer_restores_publish_hooks_from_source(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Node installer test")

    plugin_root = tmp_path / "plugin"
    hooks = plugin_root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "hooks.json").write_text('{"hooks":{"SessionStart":[]}}\n')

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        f"installer.restorePublishHooksFromSource({json.dumps(str(plugin_root))});"
    )
    result = subprocess.run(
        [node, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    restored = json.loads((hooks / "hooks.json").read_text())["hooks"]
    assert "Stop" in restored
    assert "SessionEnd" in restored


def test_node_install_reads_managed_env_and_bootstraps_latest_cache(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Node installer test")

    cache_root = (
        tmp_path / ".claude" / "plugins" / "cache" / "reflexioai" / "claude-smart"
    )
    for version in ("0.2.31", "0.2.32"):
        plugin_root = cache_root / version
        scripts = plugin_root / "scripts"
        scripts.mkdir(parents=True)
        (plugin_root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
        install = scripts / "smart-install.sh"
        install.write_text(
            "#!/bin/sh\n"
            '[ "$CLAUDE_SMART_MANAGED_SETUP" = "1" ] || exit 41\n'
            '[ "$REFLEXIO_API_KEY" = "rflx-test-secret" ] || exit 42\n'
            "printf 'bootstrapped %s\\n' \"$PWD\"\n"
        )
        install.chmod(install.stat().st_mode | stat.S_IXUSR)

    old_root = cache_root / "0.2.31"
    new_root = cache_root / "0.2.32"
    old_mtime = new_root.stat().st_mtime + 100
    os.utime(old_root, (old_mtime, old_mtime))

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text(
        '#!/bin/sh\nprintf \'claude %s\\n\' "$*" >> "$HOME/claude.log"\nexit 0\n'
    )
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    env = _isolated_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="rflx-test-secret"\n'
    )

    result = subprocess.run(
        [node, str(NODE_INSTALLER), "install"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Using managed Reflexio" in result.stdout
    assert f"Prepared claude-smart runtime at {new_root}" in result.stdout
    env_text = env_path.read_text()
    assert 'REFLEXIO_URL="https://www.reflexio.ai/"' in env_text
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in env_text
    assert "REFLEXIO_USER_ID=" not in env_text
    assert (tmp_path / ".reflexio" / "plugin-root").resolve() == new_root


def test_npx_install_reads_managed_env(tmp_path: Path) -> None:
    npm = shutil.which("npm")
    npx = shutil.which("npx")
    if not npm or not npx:
        pytest.skip("npm and npx are required for npx installer test")

    package_info = json.loads((REPO_ROOT / "package.json").read_text())
    version = package_info["version"]
    cache_root = (
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / version
    )
    scripts = cache_root / "scripts"
    scripts.mkdir(parents=True)
    (cache_root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    install = scripts / "smart-install.sh"
    install.write_text(
        "#!/bin/sh\n"
        '[ "$CLAUDE_SMART_MANAGED_SETUP" = "1" ] || exit 41\n'
        '[ "$REFLEXIO_API_KEY" = "rflx-test-secret" ] || exit 42\n'
    )
    install.chmod(install.stat().st_mode | stat.S_IXUSR)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)

    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    pack = subprocess.run(
        [npm, "pack", "--pack-destination", str(pack_dir), "--silent"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    package_path = pack_dir / pack.stdout.strip()

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["npm_config_cache"] = str(tmp_path / "npm-cache")
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="rflx-test-secret"\n'
    )

    result = subprocess.run(
        [
            npx,
            "-y",
            "--package",
            str(package_path),
            "claude-smart",
            "install",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Using managed Reflexio" in result.stdout
    env_text = env_path.read_text()
    assert 'REFLEXIO_URL="https://www.reflexio.ai/"' in env_text
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in env_text
    assert "REFLEXIO_USER_ID=" not in env_text
    assert "CLAUDE_SMART_USE_LOCAL_CLI" not in env_text


def test_node_update_reads_managed_env(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Node installer test")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _seed_fake_claude_cache(tmp_path)
    claude = fake_bin / "claude"
    claude.write_text(_fake_claude_install_script())
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    bash = fake_bin / "bash"
    bash.write_text(
        '#!/bin/sh\nprintf \'bash %s\\n\' "$*" >> "$HOME/bash.log"\nexit 0\n'
    )
    bash.chmod(bash.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env_path = tmp_path / ".reflexio" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\nREFLEXIO_API_KEY="rflx-test-secret"\n'
    )

    result = subprocess.run(
        [node, str(NODE_INSTALLER), "update"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Updating claude-smart by reinstalling from this package" in result.stdout
    assert "claude-smart installed and dependencies are prepared" in result.stdout
    assert "Using managed Reflexio" in result.stdout
    claude_log = (tmp_path / "claude.log").read_text()
    assert "claude plugin marketplace add" in claude_log
    assert "claude plugin install claude-smart@reflexioai" in claude_log
    assert "claude plugin update" not in claude_log
    env_text = env_path.read_text()
    assert 'REFLEXIO_URL="https://www.reflexio.ai/"' in env_text
    assert 'REFLEXIO_API_KEY="rflx-test-secret"' in env_text
    assert "REFLEXIO_USER_ID=" not in env_text


def test_node_update_retries_install_after_uninstall(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Node installer test")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _seed_fake_claude_cache(tmp_path)
    claude = fake_bin / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        'printf \'claude %s\\n\' "$*" >> "$HOME/claude.log"\n'
        'if [ "$1 $2 $3" = "plugin install claude-smart@reflexioai" ]; then\n'
        '  count_file="$HOME/install-count"\n'
        "  count=0\n"
        '  [ -f "$count_file" ] && count=$(cat "$count_file")\n'
        "  count=$((count + 1))\n"
        '  printf \'%s\\n\' "$count" > "$count_file"\n'
        '  [ "$count" -eq 1 ] && exit 17\n'
        "fi\n"
        "exit 0\n"
    )
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    bash = fake_bin / "bash"
    bash.write_text(
        '#!/bin/sh\nprintf \'bash %s\\n\' "$*" >> "$HOME/bash.log"\nexit 0\n'
    )
    bash.chmod(bash.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [node, str(NODE_INSTALLER), "update"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "retrying after uninstalling claude-smart@reflexioai" in result.stderr
    assert (tmp_path / "install-count").read_text().strip() == "2"
    claude_log = (tmp_path / "claude.log").read_text().splitlines()
    assert claude_log == [
        f"claude plugin marketplace add {REPO_ROOT}",
        "claude plugin install claude-smart@reflexioai",
        "claude plugin uninstall claude-smart@reflexioai",
        "claude plugin install claude-smart@reflexioai",
    ]


def test_dashboard_service_loads_reflexio_env_for_managed_proxy() -> None:
    dashboard = (REPO_ROOT / "plugin" / "scripts" / "dashboard-service.sh").read_text()

    assert "claude_smart_source_reflexio_env" in dashboard


def test_dashboard_health_exposes_plugin_identity_for_stale_process_detection() -> None:
    route = HEALTH_ROUTE.read_text()

    assert 'export const dynamic = "force-dynamic"' in route
    assert "realpathSync" in route
    assert "x-claude-smart-dashboard" in route
    assert "x-claude-smart-plugin-root" in route
    assert "x-claude-smart-dashboard-dir" in route
    assert "x-claude-smart-version" in route


def test_dashboard_service_restarts_stale_claude_smart_dashboard() -> None:
    dashboard = (REPO_ROOT / "plugin" / "scripts" / "dashboard-service.sh").read_text()

    assert "dashboard_matches_current_root()" in dashboard
    assert "x-claude-smart-plugin-root" in dashboard
    assert "curl -sfI --connect-timeout 2 --max-time 5" in dashboard
    assert "normalize_identity_path()" in dashboard
    assert "cygpath -u" in dashboard
    assert 'expected_root="$(normalize_identity_path ' in dashboard
    assert 'actual_root="$(normalize_identity_path "$actual_root")"' in dashboard
    assert '[ "$actual_root" = "$expected_root" ]' in dashboard
    assert "stale claude-smart dashboard on port $PORT; restarting" in dashboard
    assert "stop_dashboard_listener" in dashboard
    assert "foreign app on 3001 is never killed" in dashboard


def test_installers_start_backend_and_refresh_dashboard_services() -> None:
    node_installer = NODE_INSTALLER.read_text()
    smart_install = SMART_INSTALL.read_text()

    assert 'bash "$HERE/backend-service.sh" start' in smart_install
    assert "start_backend_service()" in smart_install
    assert "if install_complete; then\n  start_backend_service" in smart_install
    assert "Backend started; dashboard auto-starts on session start." in smart_install
    assert "function startBackendService(pluginRoot, host)" in node_installer
    assert "CLAUDE_SMART_HOST: host" in node_installer
    assert "function refreshDashboardService(pluginRoot)" in node_installer
    assert "const PLUGIN_SERVICE_TIMEOUT_MS = 15_000" in node_installer
    assert "timeout: PLUGIN_SERVICE_TIMEOUT_MS" in node_installer
    assert 'killSignal: "SIGTERM"' in node_installer
    assert 'result.error && result.error.code === "ETIMEDOUT"' in node_installer
    assert (
        'runPluginService(pluginRoot, "backend-service.sh", "start"' in node_installer
    )
    assert 'startBackendService(pluginRoot, "claude-code")' in node_installer
    assert 'startBackendService(cacheDir, "codex")' in node_installer
    assert (
        'runPluginService(pluginRoot, "dashboard-service.sh", "stop")' in node_installer
    )
    assert (
        'runPluginService(pluginRoot, "dashboard-service.sh", "start")'
        in node_installer
    )
    assert "function stopClaudeSmartServices(pluginRoot)" in node_installer
    assert "Started claude-smart backend service." in node_installer
    assert "Refreshed claude-smart dashboard service." in node_installer


def test_node_update_reinstalls_by_host() -> None:
    node_installer = NODE_INSTALLER.read_text()

    assert 'if (parseHost(args) === "codex")' in node_installer
    assert "await runUpdateCodex(args)" in node_installer
    assert "async function runUpdateCodex(args)" in node_installer
    assert 'findCodexPluginRoot() || join(PACKAGE_ROOT, "plugin")' in node_installer
    assert (
        "await runInstall(args, { retryInstallAfterUninstall: true })" in node_installer
    )
    assert "await runInstallCodex(args)" in node_installer
    assert 'runClaude(["plugin", "update", PLUGIN_SPEC]' not in node_installer
    assert "update --host codex" in node_installer


def test_find_claude_code_plugin_root_does_not_fall_back_to_npx_tree() -> None:
    """Regression: findClaudeCodePluginRoot used to fall back to
    `PACKAGE_ROOT/plugin` (the npm/npx scratch tree). When npm pruned that
    tree between invocations, ~/.reflexio/plugin-root pointed at a path
    whose .venv had no python and whose claude_smart editable .pth dangled.
    Make sure we never re-introduce that fallback."""
    node_installer = NODE_INSTALLER.read_text()

    assert "function findClaudeCodePluginRoot()" in node_installer
    fn_start = node_installer.index("function findClaudeCodePluginRoot()")
    # The next top-level `function ` after this one bounds the body.
    fn_end = node_installer.index("\nfunction ", fn_start + 1)
    body = node_installer[fn_start:fn_end]
    assert 'join(PACKAGE_ROOT, "plugin")' not in body, (
        "findClaudeCodePluginRoot must not fall back to PACKAGE_ROOT/plugin; "
        "the npx tree is ephemeral and breaks editable claude_smart installs."
    )
    assert "claude plugin install did not populate" in node_installer, (
        "bootstrapClaudeCodeInstall must surface a clear error when the cache is empty"
    )


def test_smart_install_recreates_corrupt_venv() -> None:
    """Regression: when an earlier install left .venv without a python
    binary (interrupted run, npm pruning, etc.), `uv sync` errored with
    "not a valid Python environment" and never self-healed. smart-install.sh
    must detect this and clear the .venv so uv can rebuild it."""
    smart_install = SMART_INSTALL.read_text()

    assert "has no python executable; recreating" in smart_install
    assert 'rm -rf "$PLUGIN_ROOT/.venv"' in smart_install


def test_cli_sh_self_heals_when_claude_smart_unimportable() -> None:
    """Regression: cli.sh used to exec `python -m claude_smart.cli` even when
    the package wasn't importable, leaking a bare ModuleNotFoundError into
    the slash-command transcript when SessionStart's background install
    hadn't finished. It must now mirror hook_entry.sh's import probe."""
    cli_sh = (REPO_ROOT / "plugin" / "scripts" / "cli.sh").read_text()

    assert "claude_smart_python_imports" in cli_sh
    assert "claude_smart.cli" in cli_sh
    assert "claude-smart is not installed correctly" in cli_sh


def test_cli_sh_suppresses_installer_hook_payload_on_self_heal(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    scripts = plugin_root / "scripts"
    venv_bin = plugin_root / ".venv" / "bin"
    bin_dir = tmp_path / "bin"
    scripts.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(LIB, scripts / "_lib.sh")
    shutil.copy2(CLI_SH, scripts / "cli.sh")
    (scripts / "smart-install.sh").write_text(
        "#!/bin/sh\n"
        'printf \'{"continue":true,"suppressOutput":true}\\n\'\n'
        "printf 'installer diagnostic\\n' >&2\n"
        'touch "$HOME/import-ok"\n'
    )
    (venv_bin / "python").write_text(
        '#!/bin/sh\n[ -f "$HOME/import-ok" ] && exit 0\nexit 1\n'
    )
    (bin_dir / "uv").write_text("#!/bin/sh\nprintf 'cli ran\\n'\nexit 0\n")
    for executable in [
        scripts / "cli.sh",
        scripts / "smart-install.sh",
        venv_bin / "python",
        bin_dir / "uv",
    ]:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        [str(scripts / "cli.sh"), "show"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "cli ran\n"
    assert "installer diagnostic" in result.stderr
    assert '"continue"' not in result.stderr


def test_service_start_scripts_guard_internal_invocations() -> None:
    lib = (REPO_ROOT / "plugin" / "scripts" / "_lib.sh").read_text()
    hook_entry = HOOK_ENTRY.read_text()
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()
    dashboard = (REPO_ROOT / "plugin" / "scripts" / "dashboard-service.sh").read_text()

    assert "claude_smart_is_internal_invocation_env()" in lib
    assert "CLAUDE_CODE_ENTRYPOINT" in lib
    assert '"cli"|"claude-desktop"' in lib
    assert "if claude_smart_is_internal_invocation_env; then" in hook_entry
    assert "if claude_smart_is_internal_invocation_env; then" in backend
    assert "if claude_smart_is_internal_invocation_env; then" in dashboard


def test_claude_code_install_uses_bundled_package_root() -> None:
    """Regression guard: Claude Code installer must point `claude plugin marketplace
    add` at the npm package root (which carries the bundled plugin), not a GitHub
    owner/repo ref. Otherwise `make package` no longer end-to-end tests local
    plugin changes for Claude Code."""
    node_installer = CLAUDE_SMART_BIN.read_text()
    py_cli = (REPO_ROOT / "plugin" / "src" / "claude_smart" / "cli.py").read_text()

    # Node installer: source must be PACKAGE_ROOT, not a GitHub ref.
    assert "const source = PACKAGE_ROOT;" in node_installer
    assert "DEFAULT_MARKETPLACE_SOURCE" not in node_installer
    assert "parseSource(" not in node_installer
    assert '"ReflexioAI/claude-smart"' not in node_installer
    assert "--source" not in node_installer

    # Python CLI mirrors it: passes _REPO_ROOT to `claude plugin marketplace add`.
    assert '["claude", "plugin", "marketplace", "add", str(_REPO_ROOT)]' in py_cli
    assert "_DEFAULT_MARKETPLACE_SOURCE" not in py_cli
    assert '"--source"' not in py_cli


def test_reflexio_release_sync_has_strict_release_checks() -> None:
    sync_script = SYNC_REFLEXIO_DEP.read_text()
    release_script = RELEASE_WITH_REFLEXIO.read_text()

    assert "--release-checks" in sync_script
    assert 'fetch", "origin", "main", "--tags' in sync_script
    assert 'rev-parse", "origin/main' in sync_script
    assert 'tag", "--points-at", "HEAD' in sync_script
    assert "v{version}" in sync_script
    assert "PyPI reflexio-ai dependency" in sync_script
    assert '"source": "pypi"' in sync_script
    assert 'REFLEXIO_PATH="${REFLEXIO_PATH:-$REPO_ROOT/../reflexio}"' in release_script
    assert '--reflexio-path "$REFLEXIO_PATH"' in release_script
    assert "--check-pypi" in release_script
    assert "--release-checks" in release_script


def test_reflexio_vendor_release_uses_generated_bundle() -> None:
    vendor_script = VENDOR_REFLEXIO.read_text()
    release_script = RELEASE_WITH_REFLEXIO.read_text()
    lock_script = CHECK_REFLEXIO_LOCK.read_text()
    installer = NODE_INSTALLER.read_text()
    smart_install = SMART_INSTALL.read_text()
    lib = LIB.read_text()
    gitignore = (REPO_ROOT / ".gitignore").read_text()

    assert "plugin/vendor/reflexio" in vendor_script
    assert 'git", "-C", str(reflexio_path), "archive"' in vendor_script
    assert '"source": "vendor"' in vendor_script
    assert '"vendor_path": str(VENDOR_PATH)' in vendor_script
    assert "package_include_paths" in vendor_script
    assert "only-include" in vendor_script
    assert (
        'REFLEXIO_RELEASE_SOURCE="${REFLEXIO_RELEASE_SOURCE:-vendor}"' in release_script
    )
    assert 'PYTHON_BIN="${PYTHON:-python3}"' in release_script
    assert '"$PYTHON_BIN" scripts/vendor-reflexio.py' in release_script
    assert "uv pip install --project plugin --python" in release_script
    assert "-e plugin/vendor/reflexio" in release_script
    assert "OK: npm tarball includes vendored Reflexio files" in release_script
    assert "Keep generated plugin/vendor/reflexio in place" in release_script
    assert "make release-npm VERSION=<new-claude-smart-version>" in release_script
    assert "VALID_SOURCES" in lock_script
    assert "source=pypi must not include vendor_path" in lock_script
    assert "vendored Reflexio version mismatch" in lock_script
    assert "read_lock_file(lock_path)" in lock_script
    assert "reflexio.lock.json is not valid JSON" in lock_script
    assert "top-level value must be a JSON object" in lock_script
    assert "vendor_path must stay within repo root" in lock_script
    assert "installVendoredReflexio(pluginRoot, uv, env)" in installer
    assert 'join(pluginRoot, "vendor", "reflexio")' in installer
    assert '"pip", "install", "--project", pluginRoot' in installer
    assert 'VENDORED_REFLEXIO="$PLUGIN_ROOT/vendor/reflexio"' in smart_install
    assert (
        'uv pip install --project "$PLUGIN_ROOT" --python "$PLUGIN_PYTHON" '
        '--quiet -e "$VENDORED_REFLEXIO"'
    ) in smart_install
    assert "vendored Reflexio install failed" in smart_install
    assert "install_vendored_reflexio" in smart_install
    assert "vendor_reflexio_pyproject" in lib
    assert "/plugin/vendor/" in gitignore


def test_check_reflexio_lock_reports_invalid_json(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    plugin = tmp_path / "plugin"
    scripts.mkdir()
    plugin.mkdir()
    shutil.copy2(CHECK_REFLEXIO_LOCK, scripts / "check-reflexio-lock.py")
    (plugin / "pyproject.toml").write_text(
        '[project]\ndependencies = ["reflexio-ai>=0.2.22"]\n'
    )
    (tmp_path / "reflexio.lock.json").write_text("{broken")

    result = subprocess.run(
        [sys.executable, str(scripts / "check-reflexio-lock.py")],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "error: reflexio.lock.json is not valid JSON" in result.stderr
    assert "Traceback" not in result.stderr


def test_check_reflexio_lock_requires_json_object(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    plugin = tmp_path / "plugin"
    scripts.mkdir()
    plugin.mkdir()
    shutil.copy2(CHECK_REFLEXIO_LOCK, scripts / "check-reflexio-lock.py")
    (plugin / "pyproject.toml").write_text(
        '[project]\ndependencies = ["reflexio-ai>=0.2.22"]\n'
    )
    (tmp_path / "reflexio.lock.json").write_text("[]\n")

    result = subprocess.run(
        [sys.executable, str(scripts / "check-reflexio-lock.py")],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "top-level value must be a JSON object" in result.stderr
    assert "Traceback" not in result.stderr


def test_check_reflexio_lock_rejects_out_of_repo_vendor_path(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    plugin = tmp_path / "plugin"
    outside_vendor = tmp_path.parent / "outside-reflexio"
    scripts.mkdir()
    plugin.mkdir()
    outside_vendor.mkdir()
    shutil.copy2(CHECK_REFLEXIO_LOCK, scripts / "check-reflexio-lock.py")
    dependency = "reflexio-ai>=0.2.22"
    (plugin / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["{dependency}"]\n'
    )
    (tmp_path / "reflexio.lock.json").write_text(
        json.dumps(
            {
                "package": "reflexio-ai",
                "repo": "https://github.com/ReflexioAI/reflexio.git",
                "version": "0.2.22",
                "commit": "a" * 40,
                "dependency": dependency,
                "source": "vendor",
                "vendor_path": str(outside_vendor),
                "updated_at": "2026-05-24T00:00:00Z",
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(scripts / "check-reflexio-lock.py"), "--check-vendor"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "error: vendor_path must stay within repo root" in result.stderr
    assert "Traceback" not in result.stderr


def test_smart_install_installs_vendored_reflexio(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    scripts = plugin_root / "scripts"
    vendor = plugin_root / "vendor" / "reflexio"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    vendor.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(SMART_INSTALL, scripts / "smart-install.sh")
    shutil.copy2(LIB, scripts / "_lib.sh")
    shutil.copy2(
        REPO_ROOT / "plugin" / "scripts" / "ensure-plugin-root.sh",
        scripts / "ensure-plugin-root.sh",
    )
    (plugin_root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    (plugin_root / "uv.lock").write_text("")
    (vendor / "pyproject.toml").write_text("[project]\nname='reflexio-ai'\n")

    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$HOME/uv.log"\n'
        'if [ "$1" = "sync" ]; then\n'
        '  mkdir -p "$PWD/.venv/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$PWD/.venv/bin/python"\n'
        '  chmod +x "$PWD/.venv/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "python" ] && [ "$2" = "find" ]; then\n'
        '  printf "%s\\n" "$PWD/.venv/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "pip" ] && [ "$2" = "install" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", str(scripts / "smart-install.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()
    assert (tmp_path / ".claude-smart" / "install-complete").exists()
    assert "installing bundled Reflexio source" in result.stderr
    uv_log = (tmp_path / "uv.log").read_text()
    assert "sync --locked --python 3.12 --quiet" in uv_log
    assert f"pip install --project {plugin_root}" in uv_log
    assert f"-e {vendor}" in uv_log


@pytest.mark.parametrize(
    "copy_dirname",
    [
        # Windows cwd C:\Users\Alice\repo with ':' and '\' stripped by the host
        # (issue #65). Capital 'U' — a `Cu*` glob would miss this.
        "CUsersAliceRepo",
        # A different drive/dir: D:\repos\proj -> Dreposproj. No `Cu`/`CUsers`
        # prefix at all; only structural (under ~/.reflexio) detection catches it.
        "Dreposproj",
        # Lowercased host mangling, to be casing-agnostic.
        "cusersbobwork",
    ],
)
def test_smart_install_redirects_reflexio_stray_copies_to_stable_root(
    tmp_path: Path,
    copy_dirname: str,
) -> None:
    """Regression: a host can expose CLAUDE_PLUGIN_ROOT as a cwd-derived copy
    physically under ~/.reflexio (e.g. ``CUsers...`` on Windows, issue #65).
    Installing .venv/node_modules there duplicates the full runtime per
    project; smart-install must instead use the stable plugin root recorded in
    ~/.reflexio/plugin-root. Detection is structural (any plugin dir under
    ~/.reflexio), so it must hold regardless of the mangled name's prefix/case.
    """
    session_root = tmp_path / ".reflexio" / copy_dirname / "plugin"
    stable_root = tmp_path / ".claude" / "plugins" / "cache" / "reflexioai" / "claude-smart" / "0.2.42"
    fake_bin = tmp_path / "bin"
    for root in (session_root, stable_root):
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SMART_INSTALL, scripts / "smart-install.sh")
        shutil.copy2(LIB, scripts / "_lib.sh")
        shutil.copy2(
            REPO_ROOT / "plugin" / "scripts" / "ensure-plugin-root.sh",
            scripts / "ensure-plugin-root.sh",
        )
        (root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
        (root / "uv.lock").write_text("")

    (tmp_path / ".reflexio").mkdir(exist_ok=True)
    (tmp_path / ".reflexio" / "plugin-root").symlink_to(
        stable_root,
        target_is_directory=True,
    )
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s|%s\\n" "$PWD" "$*" >> "$HOME/uv.log"\n'
        'if [ "$1" = "sync" ]; then\n'
        '  mkdir -p "$PWD/.venv/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$PWD/.venv/bin/python"\n'
        '  chmod +x "$PWD/.venv/bin/python"\n'
        "fi\n"
        "exit 0\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            str(session_root / "scripts" / "smart-install.sh"),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (session_root / ".venv").exists()
    assert (stable_root / ".venv" / "bin" / "python").exists()
    uv_log = (tmp_path / "uv.log").read_text()
    assert f"{stable_root}|sync --locked --python 3.12 --quiet" in uv_log
    assert str(session_root) not in uv_log


def test_smart_install_repairs_reflexio_template_env_drift(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    scripts = plugin_root / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(SMART_INSTALL, scripts / "smart-install.sh")
    shutil.copy2(LIB, scripts / "_lib.sh")
    shutil.copy2(
        REPO_ROOT / "plugin" / "scripts" / "ensure-plugin-root.sh",
        scripts / "ensure-plugin-root.sh",
    )
    (plugin_root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    (plugin_root / "uv.lock").write_text("")

    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$HOME/uv.log"\n'
        'if [ "$1" = "sync" ]; then\n'
        '  mkdir -p "$PWD/.venv/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$PWD/.venv/bin/python"\n'
        '  chmod +x "$PWD/.venv/bin/python"\n'
        "fi\n"
        "exit 0\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    command = ["/bin/bash", "--noprofile", "--norc", str(scripts / "smart-install.sh")]

    first = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert (tmp_path / ".claude-smart" / "install-complete").exists()

    reflexio_env = tmp_path / ".reflexio" / ".env"
    reflexio_env.write_text(
        "# Reflexio template-style env without claude-smart local defaults\n"
        "OPENAI_API_KEY=\n"
        "ANTHROPIC_API_KEY=\n"
    )

    second = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    env_text = reflexio_env.read_text()
    assert "OPENAI_API_KEY=" in env_text
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in env_text
    assert "CLAUDE_SMART_USE_LOCAL_EMBEDDING=1" in env_text
    assert 'CLAUDE_SMART_READ_ONLY="0"' in env_text
    assert (tmp_path / "uv.log").read_text().count(
        "sync --locked --python 3.12 --quiet"
    ) == 2


def test_vendor_release_is_npm_only() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    developer = (REPO_ROOT / "DEVELOPER.md").read_text()

    assert "release-npm:" in makefile
    assert "check-pypi-compatible-reflexio:" in makefile
    assert "source=vendor" in makefile
    assert "make release-npm VERSION=..." in makefile
    assert "make release-npm VERSION=<new-claude-smart-version>" in developer
    assert "intentionally refuses to publish PyPI" in developer


def test_backend_service_configures_shared_embedding_daemon() -> None:
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'EMBEDDING_PORT="${EMBEDDING_PORT:-8072}"' in backend
    assert "REFLEXIO_EMBEDDING_PROVIDER:-local_service" in backend
    assert "REFLEXIO_EMBEDDING_SERVICE_URL" in backend


def test_backend_service_full_stop_reaps_embedding_port() -> None:
    """full_stop must sweep both the backend port and the embedding port.

    Without this, a stale embedding service (uvicorn
    reflexio.server.llm.embedding_service) can survive ``/claude-smart:restart``
    and block subsequent boots when something else is squatting 8071.
    """
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'reap_port_listeners "$PORT"' in backend
    assert 'reap_port_listeners "$EMBEDDING_PORT"' in backend
    assert "'*reflexio.server.llm.embedding_service:app*'" in backend
    assert "'*reflexio*embedding_service*'" in backend
    assert "'*embedding_service*'" not in backend
    assert (
        "reap_port_listeners \"$EMBEDDING_PORT\" '*reflexio*' '*uvicorn*'"
        not in backend
    )


def test_backend_service_surfaces_port_holder_on_skip() -> None:
    """When start skips because the port is held, the holder must be reported.

    Silently skipping makes ``/claude-smart:restart`` look successful even
    though the backend never came up — users need to see which process is
    squatting the port so they can free it.
    """
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert "port_holder()" in backend
    assert 'holder="$(port_holder "$PORT"' in backend
    assert "Free port $PORT" in backend


def test_codex_hook_recovers_missing_dependencies_without_cli_command() -> None:
    script = CODEX_HOOK.read_text()

    assert "function startInstallerDetached(root, reason)" in script
    assert 'startInstallerDetached(root, "backend: uv not on PATH")' in script
    assert 'startInstallerDetached(root, "dashboard: npm not on PATH")' in script
    assert 'startInstallerDetached(root, "hook: uv not on PATH")' in script


def test_smart_install_waits_on_existing_lock() -> None:
    script = SMART_INSTALL.read_text()

    assert "flock -n" not in script
    assert "flock 9" in script
    assert 'INSTALL_REAP_LOCK="$MARKER_DIR/install.lock.reap"' in script
    assert 'mkdir "$INSTALL_REAP_LOCK"' in script
    assert "set -C" in script
    assert 'remove_stale_install_lock "$lock_pid"' in script
    assert '[ "$(cat "$INSTALL_LOCK" 2>/dev/null || true)" = "$$" ]' in script
    assert 'kill -0 "$lock_pid"' in script


def test_smart_install_waits_on_portable_lock_without_flock(tmp_path: Path) -> None:
    bin_dir = Path(
        _minimal_path(tmp_path, "dirname", "mkdir", "rm", "cat", "sleep", "sed", "awk")
    )
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
    bin_dir = Path(
        _minimal_path(
            tmp_path, "dirname", "mkdir", "rm", "rmdir", "cat", "sleep", "sed", "awk"
        )
    )
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
    setup_block = hooks["hooks"]["Setup"][0]
    runtime_block = hooks["hooks"]["SessionStart"][0]

    assert "smart-install.sh" in setup_block["hooks"][0]["command"]
    assert runtime_block["matcher"] == "startup|clear|compact|resume"
    assert all(
        "smart-install.sh" not in hook["command"] for hook in runtime_block["hooks"]
    )
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
    hooks = json.loads(
        (REPO_ROOT / "plugin" / "hooks" / "codex-hooks.json").read_text()
    )
    session_hooks = hooks["hooks"]["SessionStart"][0]["hooks"]
    hook_entry = next(
        hook for hook in session_hooks if "hook_entry.sh" in hook["command"]
    )
    backend_hook = next(
        hook for hook in session_hooks if "backend-service.sh" in hook["command"]
    )
    dashboard_hook = next(
        hook for hook in session_hooks if "dashboard-service.sh" in hook["command"]
    )

    assert hook_entry["timeout"] == 300
    assert backend_hook["timeout"] == 300
    assert dashboard_hook["timeout"] == 300


def test_hook_entry_self_heals_missing_uv_without_cli_command() -> None:
    script = HOOK_ENTRY.read_text()

    assert "claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1" in script
    assert 'bash "$PLUGIN_ROOT/scripts/smart-install.sh"' in script
    assert 'bash "$HERE/backend-service.sh" start' not in script
    assert 'bash "$HERE/dashboard-service.sh" start' not in script


def test_install_complete_requires_importable_hook_package() -> None:
    script = SMART_INSTALL.read_text()

    assert (
        'claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.hook || return 1'
        in script
    )
    assert (
        'if ! claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.hook; then'
        in script
    )


def test_hook_entry_skips_cleanly_when_cached_package_missing(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    scripts = plugin_root / "scripts"
    venv_bin = plugin_root / ".venv" / "bin"
    bin_dir = tmp_path / "bin"
    scripts.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(LIB, scripts / "_lib.sh")
    shutil.copy2(HOOK_ENTRY, scripts / "hook_entry.sh")
    (scripts / "smart-install.sh").write_text(
        "#!/bin/sh\n"
        "printf 'installer should not run while bootstrapping\\n' >> \"$HOME/install.log\"\n"
    )
    (venv_bin / "python").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "uv").write_text(
        "#!/bin/sh\n"
        "printf 'uv should not execute broken hook package\\n' >&2\n"
        "exit 17\n"
    )
    for executable in [
        scripts / "hook_entry.sh",
        scripts / "smart-install.sh",
        venv_bin / "python",
        bin_dir / "uv",
    ]:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CLAUDE_SMART_BOOTSTRAPPING"] = "1"
    result = subprocess.run(
        [str(scripts / "hook_entry.sh"), "claude-code", "stop"],
        env=env,
        text=True,
        input=json.dumps({"session_id": "s1"}),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"continue": True, "suppressOutput": True}
    assert not (tmp_path / "install.log").exists()
    assert "uv should not execute" not in result.stderr


def test_hook_entry_failure_marker_uses_resolved_python_on_windows(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "plugin"
    scripts = plugin_root / "scripts"
    bin_dir = tmp_path / "bin"
    scripts.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(LIB, scripts / "_lib.sh")
    shutil.copy2(HOOK_ENTRY, scripts / "hook_entry.sh")
    (plugin_root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")
    (plugin_root / "uv.lock").write_text("")
    (bin_dir / "uname").write_text("#!/bin/sh\nprintf 'MINGW64_NT-10.0\\n'\n")
    (bin_dir / "python3").write_text(
        "#!/bin/sh\n"
        "printf 'python3 stub should not be used\\n' >&2\n"
        'touch "$HOME/python3-called"\n'
        "exit 99\n"
    )
    (bin_dir / "python").write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '%s\\n' "
        '\'{"hookSpecificOutput":{"hookEventName":"SessionStart",'
        '"additionalContext":"> **claude-smart is not installed correctly:** '
        "cached env is broken\"}}}'\n"
    )
    for executable in [
        scripts / "hook_entry.sh",
        bin_dir / "uname",
        bin_dir / "python",
        bin_dir / "python3",
    ]:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    fingerprint = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            (
                f'. "{scripts / "_lib.sh"}"; '
                f'claude_smart_install_fingerprint_hash "{plugin_root}" "{scripts}"'
            ),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    marker_dir = tmp_path / ".claude-smart"
    marker_dir.mkdir()
    (marker_dir / "install-failed").write_text(
        f"cached env is broken\nfingerprint={fingerprint}\n"
    )

    result = subprocess.run(
        [str(scripts / "hook_entry.sh"), "claude-code", "session-start"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "claude-smart is not installed correctly" in result.stdout
    assert not (tmp_path / "python3-called").exists()


def test_hook_entry_defaults_codex_citation_links_to_markdown(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "{\\"hookSpecificOutput\\":'
        '{\\"hookEventName\\":\\"UserPromptSubmit\\",'
        '\\"additionalContext\\":\\"$CLAUDE_SMART_CITATION_LINK_STYLE\\"}}"\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("CLAUDE_SMART_CITATION_LINK_STYLE", None)
    result = subprocess.run(
        [str(HOOK_ENTRY), "codex", "user-prompt"],
        env=env,
        text=True,
        input=json.dumps({"session_id": "s1", "prompt": "What food do I like?"}),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["hookSpecificOutput"]["additionalContext"] == "markdown"

    env["CLAUDE_SMART_CITATION_LINK_STYLE"] = "osc8"
    result = subprocess.run(
        [str(HOOK_ENTRY), "codex", "user-prompt"],
        env=env,
        text=True,
        input=json.dumps({"session_id": "s1", "prompt": "What food do I like?"}),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["hookSpecificOutput"]["additionalContext"] == "osc8"


def test_node_installer_platform_preflight_messages() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for installer wrapper tests")

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        "console.log(installer.platformSupportError() || 'ok');"
    )
    cases = [
        (
            {
                "CLAUDE_SMART_TEST_PLATFORM": "darwin",
                "CLAUDE_SMART_TEST_ARCH": "arm64",
                "CLAUDE_SMART_TEST_RELEASE": "23.0.0",
            },
            "ok",
        ),
        (
            {
                "CLAUDE_SMART_TEST_PLATFORM": "darwin",
                "CLAUDE_SMART_TEST_ARCH": "x64",
                "CLAUDE_SMART_TEST_RELEASE": "23.0.0",
            },
            "Intel Mac is not supported",
        ),
        (
            {
                "CLAUDE_SMART_TEST_PLATFORM": "darwin",
                "CLAUDE_SMART_TEST_ARCH": "arm64",
                "CLAUDE_SMART_TEST_RELEASE": "22.6.0",
            },
            "macOS 13 and older are not supported",
        ),
        (
            {"CLAUDE_SMART_TEST_PLATFORM": "win32", "CLAUDE_SMART_TEST_ARCH": "arm64"},
            "Windows ARM is not supported",
        ),
        (
            {"CLAUDE_SMART_TEST_PLATFORM": "win32", "CLAUDE_SMART_TEST_ARCH": "x64"},
            "ok",
        ),
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


def test_node_installer_codex_marketplace_cache_uses_manifest_plugin_path(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for installer wrapper tests")

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        "const root = installer.copyCodexMarketplace();"
        "const pluginRoot = installer.codexMarketplacePluginRoot(root);"
        "const fs = require('fs');"
        "const path = require('path');"
        "const manifestPath = path.join(root, '.agents/plugins/marketplace.json');"
        "const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));"
        "console.log(JSON.stringify({"
        "root,"
        "path: manifest.plugins[0].source.path,"
        "pluginRoot,"
        "hasManifest: fs.existsSync(path.join("
        "pluginRoot, '.codex-plugin/plugin.json'"
        ")),"
        "legacyPathExists: fs.existsSync(path.join(root, 'plugins/claude-smart'))"
        "}));"
    )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [node, "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["path"] == "./plugin"
    assert payload["pluginRoot"] == str(
        tmp_path / ".claude" / "plugins" / "marketplaces" / "reflexioai" / "plugin"
    )
    assert payload["hasManifest"] is True
    assert payload["legacyPathExists"] is False


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


def test_node_installer_bootstraps_runtime_with_private_node_and_uv(
    tmp_path: Path,
) -> None:
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
    # patchCodexHooksForNode can dispatch by script name.
    (hooks / "codex-hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        'bash "$_R/scripts/ensure-plugin-root.sh" "$_R"'
                                    )
                                },
                                {
                                    "command": (
                                        'bash "$_R/scripts/backend-service.sh" start'
                                    )
                                },
                                {
                                    "command": (
                                        'bash "$_R/scripts/dashboard-service.sh" start'
                                    )
                                },
                                {
                                    "command": (
                                        'bash "$_R/scripts/hook_entry.sh" codex '
                                        "session-start"
                                    )
                                },
                            ]
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        'bash "$_R/scripts/hook_entry.sh" codex '
                                        "user-prompt"
                                    )
                                }
                            ]
                        }
                    ],
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        'bash "$_R/scripts/hook_entry.sh" codex '
                                        "post-tool"
                                    )
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        'bash "$_R/scripts/hook_entry.sh" codex stop'
                                    )
                                }
                            ]
                        }
                    ],
                }
            }
        )
        + "\n"
    )
    (private_node / "node").write_text("#!/bin/sh\nexit 0\n")
    (private_node / "npm").write_text(
        '#!/bin/sh\nprintf \'npm %s\\n\' "$*" >> "$HOME/npm.log"\nexit 0\n'
    )
    (uv_bin / "uv").write_text(
        '#!/bin/sh\nprintf \'uv %s\\n\' "$*" >> "$HOME/uv.log"\nexit 0\n'
    )
    for executable in [private_node / "node", private_node / "npm", uv_bin / "uv"]:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        f"installer.bootstrapPluginRuntime({json.dumps(str(plugin_root))})"
        ".then(() => console.log('done'))"
        ".catch((err) => { console.error(err.message); process.exit(1); });"
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
    # Each SessionStart hook dispatches to node by command content. Verify each maps to
    # the right codex-hook.js subcommand and references the private node.
    # Each arg is independently shell-quoted, so the sub-event appears as
    # `"hook" "session-start"` rather than `hook session-start`.
    expected_subs = [
        ["ensure-root"],
        ["backend"],
        ["dashboard"],
        ["hook", "session-start"],
    ]
    for idx, sub_tokens in enumerate(expected_subs):
        cmd = session_hooks[idx]["command"]
        assert str(private_node / "node") in cmd, f"index {idx}: {cmd}"
        assert "codex-hook.js" in cmd, f"index {idx}: {cmd}"
        for token in sub_tokens:
            assert f'"{token}"' in cmd, f"index {idx} expected token {token!r}: {cmd}"
    user_prompt_cmd = parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert '"hook"' in user_prompt_cmd and '"user-prompt"' in user_prompt_cmd


def test_node_installer_read_only_prunes_publish_hooks(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for installer wrapper tests")

    plugin_root = tmp_path / "plugin"
    hooks = plugin_root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        'bash "$_R/scripts/hook_entry.sh" '
                                        "claude-code session-start"
                                    )
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        'bash "$_R/scripts/hook_entry.sh" '
                                        "claude-code stop"
                                    )
                                }
                            ]
                        }
                    ],
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        'bash "$_R/scripts/hook_entry.sh" '
                                        "claude-code session-end"
                                    )
                                },
                                {
                                    "command": (
                                        'bash "$_R/scripts/backend-service.sh" '
                                        "session-end"
                                    )
                                },
                            ]
                        }
                    ],
                }
            }
        )
        + "\n"
    )
    (hooks / "codex-hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        '"/node" "/plugin/scripts/codex-hook.js" '
                                        '"hook" "session-start"'
                                    )
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        '"/node" "/plugin/scripts/codex-hook.js" '
                                        '"hook" "stop"'
                                    )
                                }
                            ]
                        }
                    ],
                }
            }
        )
        + "\n"
    )

    script = (
        f"const installer = require({json.dumps(str(NODE_INSTALLER))});"
        f"installer.prunePublishHooksForReadOnly({json.dumps(str(plugin_root))});"
    )
    result = subprocess.run(
        [node, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    claude_hooks = json.loads((hooks / "hooks.json").read_text())["hooks"]
    codex_hooks = json.loads((hooks / "codex-hooks.json").read_text())["hooks"]
    assert "Stop" not in claude_hooks
    assert "Stop" not in codex_hooks
    assert "SessionStart" in claude_hooks
    assert "SessionStart" in codex_hooks
    assert "backend-service.sh" in json.dumps(claude_hooks)


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
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--output-last-message" ]; then\n'
        "    shift\n"
        '    out="$1"\n'
        "  fi\n"
        "  shift || exit 1\n"
        "done\n"
        'cat > "$out.prompt"\n'
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
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--output-last-message" ]; then\n'
        "    shift\n"
        '    out="$1"\n'
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
    assert json.loads(result.stdout) == {"continue": True}


def test_codex_hook_redirects_reflexio_stray_copy_to_stable_root(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    stray_root = tmp_path / ".reflexio" / "CUsersAliceRepo" / "plugin"
    stable_root = (
        tmp_path
        / ".codex"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / "0.2.42"
    )
    for root in (stray_root, stable_root):
        (root / "scripts").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='claude-smart'\n")

    reflexio = tmp_path / ".reflexio"
    (reflexio / "plugin-root").symlink_to(stable_root, target_is_directory=True)

    env = _isolated_env(tmp_path)
    env["CLAUDE_PLUGIN_ROOT"] = str(stray_root)
    result = subprocess.run(
        [node, str(CODEX_HOOK), "ensure-root"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (reflexio / "plugin-root").resolve() == stable_root
    assert json.loads(result.stdout) == {"continue": True}
    assert not (reflexio / "plugin-root").resolve() == stray_root
    assert "redirecting stray plugin copy" in (
        tmp_path / ".claude-smart" / "backend.log"
    ).read_text()


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
    assert json.loads(result.stdout) == {"continue": True}
    assert log.stat().st_size <= 10_000_000


def test_codex_hook_post_tool_fallback_omits_suppress_output(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    env = _isolated_env(tmp_path)
    env["PATH"] = ""
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT / "plugin")
    result = subprocess.run(
        [node, str(CODEX_HOOK), "hook", "post-tool"],
        env=env,
        text=True,
        input=json.dumps({"session_id": "s1", "tool_name": "Bash"}),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"continue": True}


def test_codex_hook_reflexio_env_file_overrides_stale_managed_process_env(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    reflexio_env = tmp_path / ".reflexio" / ".env"
    reflexio_env.parent.mkdir()
    reflexio_env.write_text(
        'REFLEXIO_URL="https://www.reflexio.ai/"\n'
        'REFLEXIO_API_KEY="rflx-file-secret"\n'
        "CLAUDE_SMART_USE_LOCAL_CLI=file-local\n"
        'CLAUDE_SMART_READ_ONLY="1"\n'
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf \'{"hookSpecificOutput":{"hookEventName":"PostToolUse",'
        '\\"additionalContext\\":\\"%s|%s|%s|%s\\"}}\\n\' '
        '"$REFLEXIO_URL" "$REFLEXIO_API_KEY" '
        '"$CLAUDE_SMART_USE_LOCAL_CLI" "$CLAUDE_SMART_READ_ONLY"\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = str(bin_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT / "plugin")
    env["REFLEXIO_URL"] = "https://stale.example/"
    env["REFLEXIO_API_KEY"] = "rflx-stale-secret"
    env["CLAUDE_SMART_USE_LOCAL_CLI"] = "env-local"

    result = subprocess.run(
        [node, str(CODEX_HOOK), "hook", "post-tool"],
        env=env,
        text=True,
        input=json.dumps({"session_id": "s1", "tool_name": "Bash"}),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["hookSpecificOutput"]["additionalContext"] == (
        "https://www.reflexio.ai/|rflx-file-secret|env-local|1"
    )
    assert parsed["continue"] is True


def test_codex_hook_normalizer_removes_suppress_output_for_hooks(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"continue\":true,\"suppressOutput\":true}'\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = str(bin_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT / "plugin")
    result = subprocess.run(
        [node, str(CODEX_HOOK), "hook", "post-tool"],
        env=env,
        text=True,
        input=json.dumps({"session_id": "s1", "tool_name": "Bash"}),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"continue": True}


def test_codex_hook_defaults_citation_links_to_markdown(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "{\\"hookSpecificOutput\\":'
        '{\\"hookEventName\\":\\"UserPromptSubmit\\",'
        '\\"additionalContext\\":\\"$CLAUDE_SMART_CITATION_LINK_STYLE\\"}}"\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    env = _isolated_env(tmp_path)
    env["PATH"] = str(bin_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT / "plugin")
    env.pop("CLAUDE_SMART_CITATION_LINK_STYLE", None)
    result = subprocess.run(
        [node, str(CODEX_HOOK), "hook", "user-prompt"],
        env=env,
        text=True,
        input=json.dumps({"session_id": "s1", "prompt": "What food do I like?"}),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["hookSpecificOutput"]["additionalContext"] == "markdown"
    assert parsed["continue"] is True


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
        f'. "{LIB}"; claude_smart_install_fingerprint_hash "{plugin_root}" "{scripts}"'
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


def test_install_private_node_checksum_failure_is_dashboard_only(
    tmp_path: Path,
) -> None:
    dist = _fake_node_dist(tmp_path, bad_checksum=True)
    result = _run_private_node_install(tmp_path, f"file://{dist}")

    assert result.returncode == 1
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert marker.is_file()
    assert "checksum verification failed" in marker.read_text()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()


def test_install_private_node_download_failure_is_dashboard_only(
    tmp_path: Path,
) -> None:
    missing_dist = tmp_path / "missing-node-dist"
    result = _run_private_node_install(tmp_path, f"file://{missing_dist}")

    assert result.returncode == 1
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert marker.is_file()
    assert "could not download Node.js checksums" in marker.read_text()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()
