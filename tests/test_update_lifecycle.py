"""Real installer/update dispatch with owned fake services and runtime tooling."""

import json
import os
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_install_scripts import (
    REPO_ROOT,
    _fake_claude_code_package,
    _write_bootstrap_uv,
    _write_executable,
    _write_fake_codex,
    node_bin,
)


def _fixture(
    tmp_path: Path, host: str, *, running: str = "both", installed: bool = True
):
    package = _fake_claude_code_package(tmp_path, "#!/bin/sh\nexit 0\n")
    # The production copier and hook patcher consume genuine package manifests.
    for name in (
        ".agents/plugins/marketplace.json",
        "plugin/.codex-plugin/plugin.json",
        "plugin/hooks/codex-hooks.json",
        "plugin/scripts/opencode-claude-compat",
        "plugin/scripts/opencode-claude-compat.cmd",
        "plugin/scripts/opencode-claude-compat.js",
        "plugin/scripts/codex-hook.js",
        "plugin/scripts/_codex_env.sh",
    ):
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / name, target)
    (package / "plugin/identity").write_text("new")
    record = """import json, os, signal, sys, time
from pathlib import Path
home = Path(os.environ["HOME"])
kind, action = sys.argv[1:]
root = Path.cwd()
link = home / ".reflexio/plugin-root"
env = home / ".claude-smart/.env"
marker = home / ".claude-smart/install-failed"
row = {"kind": kind, "action": action, "root": str(root),
       "identity": (root / "identity").read_text(),
       "host": os.environ.get("CLAUDE_SMART_HOST"),
       "file": env.read_text() if env.is_file() else None,
       "selected_root": str(link.resolve()) if link.exists() else None,
       "marker": marker.read_text() if marker.is_file() else None}
with (home / "service-calls.jsonl").open("a") as out:
    out.write(json.dumps(row) + "\\n")
stopped = home / f"fixture-{kind}-stopped"
if action == "stop" and os.environ.get("FIXTURE_UNSTOPPABLE") != kind:
    stopped.touch()
if action == "status":
    running = os.environ["FIXTURE_RUNNING"]
    pre_probe = os.environ.get("FIXTURE_INITIAL_PROBE_STARTED")
    if pre_probe and kind == "backend" and not stopped.exists():
        Path(pre_probe).touch()
        time.sleep(0.5)
    if stopped.exists():
        if os.environ.get("FIXTURE_UNVERIFIABLE") == kind:
            sys.exit(1)
        if os.environ.get("FIXTURE_INTERRUPT_PROBE") == kind:
            os.kill(os.getppid(), signal.SIGTERM)
    running_here = not stopped.exists() and running in ("both", kind)
    active = "stale claude-smart backend" if os.environ.get("FIXTURE_STALE") == kind else "running"
    print(f'{active} on http://localhost:{os.environ["FIXTURE_PORT"]}' if running_here else "not running")
"""
    for name in ("backend", "dashboard"):
        _write_executable(
            package / f"plugin/scripts/{name}-service.sh",
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(record)} {name} "$1"\n',
        )
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    _write_fake_codex(fake / "codex")
    _write_executable(fake / "opencode", "#!/bin/sh\necho 1.17.0\n")
    private = tmp_path / ".claude-smart/node/current/bin"
    private.mkdir(parents=True)
    _write_executable(private / "node", "#!/bin/sh\necho v20.9.0\n")
    _write_executable(private / "npm", "#!/bin/sh\nexit 0\n")
    uv = tmp_path / ".local/bin/uv"
    uv.parent.mkdir(parents=True)
    _write_bootstrap_uv(uv, mode="non_lock_failure")
    env_file = tmp_path / ".claude-smart/.env"
    env_file.write_text(
        'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_READ_ONLY="0"\nCLAUDE_SMART_HOST="claude-code"\nCLAUDE_SMART_LEARNING_PROVIDER="codex"\n'
    )
    marker = tmp_path / ".claude-smart/install-failed"
    marker.write_text("prior marker")
    root = (
        tmp_path / ".codex/claude-smart/runtime/plugin"
        if host == "codex"
        else tmp_path / ".claude-smart/opencode/claude-smart/plugin"
    )
    if installed:
        shutil.copytree(package / "plugin", root)
        (root / "identity").write_text("old")
        (root.parent / "package.json").write_text('{"name":"claude-smart"}')
        link = tmp_path / ".reflexio/plugin-root"
        link.parent.mkdir()
        link.symlink_to(root, target_is_directory=True)
    # Do not inherit caller config, credential, host/account, or runtime paths.
    env = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "LANG")
        if key in os.environ
    }
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = str(unused.getsockname()[1])
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path / "xdg"),
        CODEX_HOME=str(tmp_path / ".codex"),
        PATH=str(fake) + os.pathsep + env.get("PATH", ""),
        CLAUDE_SMART_INSTALL_CHILD="1",
        CLAUDE_SMART_TEST_PLATFORM="linux",
        CLAUDE_SMART_TEST_ARCH="x64",
        CLAUDE_SMART_BACKEND_AUTOSTART="0",
        CLAUDE_SMART_DASHBOARD_AUTOSTART="0",
        CLAUDE_SMART_HOST="opencode",
        FIXTURE_RUNNING=running,
        FIXTURE_PORT=port,
    )
    return package, root, uv, env


def _run(tmp_path, package, host, env, command="update"):
    return subprocess.run(
        [node_bin(), str(package / "bin/claude-smart.js"), command, "--host", host],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _calls(tmp_path):
    path = tmp_path / "service-calls.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


@pytest.mark.parametrize(
    "host, failure",
    [
        ("codex", "settings"),
        ("codex", "platform"),
        ("opencode", "settings"),
        ("opencode", "platform"),
        ("opencode", "loader"),
        ("opencode", "config"),
    ],
)
def test_update_preflight_does_not_stop_services(tmp_path, host, failure):
    package, root, _, env = _fixture(tmp_path, host)
    if failure == "settings":
        path = tmp_path / ".claude-smart/.env"
        path.unlink()
        path.mkdir()
    elif failure == "platform":
        env.update(CLAUDE_SMART_TEST_PLATFORM="darwin", CLAUDE_SMART_TEST_ARCH="x64")
    elif failure == "loader":
        path = tmp_path / ".opencode/plugins/claude-smart.js"
        path.parent.mkdir(parents=True)
        path.write_text("// owned by somebody else\n")
    else:
        (tmp_path / "opencode.json").write_text('{"plugin": "invalid"}')
    result = _run(tmp_path, package, host, env)
    assert result.returncode != 0
    assert not _calls(tmp_path), result.stderr
    assert (root / "identity").read_text() == "old"


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("failure", ["UNSTOPPABLE", "UNVERIFIABLE"])
def test_update_requires_observed_shutdown_of_running_service(tmp_path, host, failure):
    package, root, uv, env = _fixture(tmp_path, host)
    _write_bootstrap_uv(uv, mode="fresh")
    env[f"FIXTURE_{failure}"] = "backend"
    link = tmp_path / ".reflexio/plugin-root"
    original_link = link.lstat().st_ino

    result = _run(tmp_path, package, host, env)

    assert result.returncode != 0
    assert "Could not confirm shutdown of the previous backend service" in result.stderr
    assert (root / "identity").read_text() == "old"
    assert link.lstat().st_ino == original_link
    assert link.resolve() == root
    assert (tmp_path / ".claude-smart/install-failed").read_text() == "prior marker"
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()
    assert [
        (row["kind"], row["identity"])
        for row in _calls(tmp_path)
        if row["action"] == "start"
    ] == (
        [("backend", "old"), ("dashboard", "old")]
        if failure == "UNVERIFIABLE"
        else [("dashboard", "old")]
    )


def test_update_refuses_shutdown_when_health_disappears_but_port_stays_open(tmp_path):
    package, root, uv, env = _fixture(tmp_path, "codex", running="backend")
    _write_bootstrap_uv(uv, mode="fresh")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        env["FIXTURE_PORT"] = str(listener.getsockname()[1])
        result = _run(tmp_path, package, "codex", env)

    assert result.returncode != 0
    assert "Could not confirm shutdown of the previous backend service" in result.stderr
    assert (root / "identity").read_text() == "old"
    assert not [row for row in _calls(tmp_path) if row["action"] == "start"]


def test_interrupt_during_post_stop_probe_recovers_observed_service(tmp_path):
    package, root, uv, env = _fixture(tmp_path, "codex", running="backend")
    _write_bootstrap_uv(uv, mode="fresh")
    env["FIXTURE_INTERRUPT_PROBE"] = "backend"

    result = _run(tmp_path, package, "codex", env)

    assert result.returncode == 143, result.stderr
    assert (root / "identity").read_text() == "old"
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()
    assert [row["kind"] for row in _calls(tmp_path) if row["action"] == "start"] == [
        "backend"
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX foreground process-group signal")
@pytest.mark.parametrize("phase", ["status", "tcp"])
def test_terminal_interrupt_preserves_previous_service(tmp_path, phase):
    package, root, uv, env = _fixture(tmp_path, "codex", running="backend")
    _write_bootstrap_uv(uv, mode="fresh")
    started = tmp_path / "tcp-probe-started"
    preload = tmp_path / "slow-probe.cjs"
    preload.write_text(
        """
        const childProcess = require("node:child_process");
        const spawnSync = childProcess.spawnSync;
        childProcess.spawnSync = function(command, args, options) {
          if (command === process.execPath && args[0] === "-e" && args[1].includes("node:net")) {
            const probe = `
              require("node:fs").writeFileSync(process.env.FIXTURE_PROBE_STARTED, "");
              setTimeout(() => { ${args[1]} }, 500);
            `;
            args = [args[0], probe, ...args.slice(2)];
          }
          return spawnSync(command, args, options);
        };
        """
    )
    if phase == "tcp":
        env["NODE_OPTIONS"] = f'--require "{preload}"'
        env["FIXTURE_PROBE_STARTED"] = str(started)
    else:
        env["FIXTURE_INITIAL_PROBE_STARTED"] = str(started)
    with subprocess.Popen(
        [node_bin(), str(package / "bin/claude-smart.js"), "update", "--host", "codex"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as installer:
        try:
            deadline = time.monotonic() + 10
            while (
                not started.exists()
                and installer.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert started.exists(), f"installer never started the {phase} check"
            os.killpg(installer.pid, signal.SIGINT)
            _, stderr = installer.communicate(timeout=10)
        finally:
            if installer.poll() is None:
                os.killpg(installer.pid, signal.SIGKILL)
                installer.communicate(timeout=10)

    assert installer.returncode == 130, stderr
    assert (root / "identity").read_text() == "old"
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()
    actions = [
        row["action"]
        for row in _calls(tmp_path)
        if row["kind"] == "backend" and row["action"] != "status"
    ]
    assert actions == (["stop", "start"] if phase == "tcp" else [])


def test_update_rejects_timeout_even_when_status_exits_zero(tmp_path):
    package, root, uv, env = _fixture(tmp_path, "codex", running="backend")
    _write_bootstrap_uv(uv, mode="fresh")
    preload = tmp_path / "timed-out-status.cjs"
    preload.write_text(
        r"""
        const {existsSync} = require("node:fs");
        const {join} = require("node:path");
        const childProcess = require("node:child_process");
        const spawnSync = childProcess.spawnSync;
        childProcess.spawnSync = function(command, args, options) {
          if (args[0].endsWith("backend-service.sh") && args[1] === "status" &&
              existsSync(join(process.env.HOME, "fixture-backend-stopped"))) {
            return { status: 0, error: {code: "ETIMEDOUT"}, stdout: "not running\n" };
          }
          return spawnSync(command, args, options);
        };
        """
    )
    env["NODE_OPTIONS"] = f'--require "{preload}"'

    result = _run(tmp_path, package, "codex", env)

    assert result.returncode != 0
    assert "Could not confirm shutdown of the previous backend service" in result.stderr
    assert (root / "identity").read_text() == "old"
    assert [row["kind"] for row in _calls(tmp_path) if row["action"] == "start"] == [
        "backend"
    ]


def test_failed_update_recovers_stopped_stale_backend(tmp_path):
    package, root, _, env = _fixture(tmp_path, "codex", running="backend")
    env["FIXTURE_STALE"] = "backend"

    result = _run(tmp_path, package, "codex", env)

    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    assert (root / "identity").read_text() == "old"
    assert [row["kind"] for row in _calls(tmp_path) if row["action"] == "start"] == [
        "backend"
    ]


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("command", ["install", "update"])
@pytest.mark.parametrize("running", ["both", "backend", "dashboard", "none"])
def test_failed_bootstrap_restores_state_before_restarting(
    tmp_path, host, command, running
):
    package, root, _, env = _fixture(tmp_path, host, running=running)
    result = _run(tmp_path, package, host, env, command)
    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    starts = [row for row in _calls(tmp_path) if row["action"] == "start"]
    expected = (
        ["backend", "dashboard"]
        if running == "both"
        else ([] if running == "none" else [running])
    )
    assert [row["kind"] for row in starts] == expected
    for row in starts:
        assert row["root"] == str(root)
        assert row["identity"] == "old"
        assert row["selected_root"] == str(root)
        assert row["marker"] == "prior marker"
        assert 'CLAUDE_SMART_HOST="claude-code"' in row["file"]
        assert 'CLAUDE_SMART_LEARNING_PROVIDER="codex"' in row["file"]
        if row["kind"] == "backend":
            assert (
                row["host"] == "opencode"
            )  # Caller wins; saved provider remains file-owned.
    assert (root / "identity").read_text() == "old"
    assert not list(root.parent.parent.glob(".claude-smart-previous*"))


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("installed", [True, False])
def test_failed_install_restores_absent_root_without_selecting_candidate(
    tmp_path, host, installed
):
    package, root, _, env = _fixture(
        tmp_path, host, installed=installed, running="none"
    )
    link = tmp_path / ".reflexio/plugin-root"
    if installed:
        link.unlink()
    result = _run(tmp_path, package, host, env)
    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    assert not link.exists() and not link.is_symlink()
    assert not (tmp_path / ".reflexio/plugin-root.txt").exists()
    assert root.exists() == installed
    assert not [row for row in _calls(tmp_path) if row["action"] == "start"]


@pytest.mark.parametrize("host", ["codex", "opencode"])
def test_successful_update_keeps_new_runtime_without_rollback_restart(tmp_path, host):
    package, root, uv, env = _fixture(tmp_path, host)
    _write_bootstrap_uv(uv, mode="fresh")
    result = _run(tmp_path, package, host, env)
    assert result.returncode == 0, result.stderr
    assert (root / "identity").read_text() == "new"
    assert not [row for row in _calls(tmp_path) if row["action"] == "start"]
    assert "Restarted claude-smart services" not in result.stderr


def test_codex_postcommit_trust_failure_keeps_new_runtime_and_cache(tmp_path):
    package, root, uv, env = _fixture(tmp_path, "codex")
    _write_bootstrap_uv(uv, mode="fresh")
    fake = tmp_path / "fake-bin/codex"
    fake.write_text(
        fake.read_text().replace(
            'currentHash: "hash-session-start"', "currentHash: null"
        )
    )
    result = _run(tmp_path, package, "codex", env)
    assert result.returncode != 0
    assert "did not report trust hashes" in result.stderr, result.stderr
    assert (root / "identity").read_text() == "new"
    caches = list((tmp_path / ".codex/plugins/cache/reflexioai/claude-smart").iterdir())
    assert len(caches) == 1
    assert (caches[0] / "identity").read_text() == "new"
    assert not [row for row in _calls(tmp_path) if row["action"] == "start"]
    assert not (tmp_path / ".claude-smart/install-failed").exists()


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("saved_host", [None, "claude-code"])
def test_restart_without_caller_host_uses_saved_host_or_existing_default(
    tmp_path, host, saved_host
):
    package, _, _, env = _fixture(tmp_path, host, running="backend")
    env["CLAUDE_SMART_HOST"] = ""
    env_file = tmp_path / ".claude-smart/.env"
    if saved_host is None:
        env_file.write_text(
            'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\nCLAUDE_SMART_READ_ONLY="0"\nCLAUDE_SMART_LEARNING_PROVIDER="codex"\n'
        )
    result = _run(tmp_path, package, host, env)
    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    starts = [row for row in _calls(tmp_path) if row["action"] == "start"]
    assert len(starts) == 1
    assert starts[0]["host"] == "claude-code"
    assert ("CLAUDE_SMART_HOST=" in starts[0]["file"]) == (saved_host is not None)


def test_codex_update_selects_newest_valid_legacy_runtime_before_source(tmp_path):
    package, root, _, env = _fixture(
        tmp_path, "codex", installed=False, running="backend"
    )
    cache = tmp_path / ".codex/plugins/cache/reflexioai/claude-smart"
    for version in ("2.0.0", "10.0.0"):
        legacy = cache / version
        shutil.copytree(package / "plugin", legacy)
        (legacy / "identity").write_text("legacy-" + version)
    newest = cache / "10.0.0"
    link = tmp_path / ".reflexio/plugin-root"
    link.parent.mkdir()
    link.symlink_to(newest, target_is_directory=True)
    result = _run(tmp_path, package, "codex", env)
    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    calls = _calls(tmp_path)
    assert {row["root"] for row in calls} == {str(newest)}
    starts = [row for row in calls if row["action"] == "start"]
    assert len(starts) == 1 and starts[0]["identity"] == "legacy-10.0.0"
    assert starts[0]["selected_root"] == str(newest)
    assert not root.exists()


@pytest.mark.parametrize("host", ["codex", "opencode"])
def test_failed_update_repairs_previously_broken_root_to_restored_install(
    tmp_path, host
):
    package, root, _, env = _fixture(tmp_path, host, running="backend")
    link = tmp_path / ".reflexio/plugin-root"
    link.unlink()
    link.symlink_to(tmp_path / "pruned-old-runtime", target_is_directory=True)
    result = _run(tmp_path, package, host, env)
    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    starts = [row for row in _calls(tmp_path) if row["action"] == "start"]
    assert len(starts) == 1
    assert starts[0]["identity"] == "old"
    assert starts[0]["selected_root"] == str(root)


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("command", ["install", "update"])
@pytest.mark.parametrize("running", ["both", "dashboard"])
def test_first_integration_failure_recovers_existing_other_integration(
    tmp_path, host, command, running
):
    package, candidate, _, env = _fixture(
        tmp_path, host, running=running, installed=False
    )
    old = tmp_path / ".claude-smart/claude-code/claude-smart/plugin"
    shutil.copytree(package / "plugin", old)
    (old / "identity").write_text("previous-claude")
    link = tmp_path / ".reflexio/plugin-root"
    link.parent.mkdir()
    link.symlink_to(old, target_is_directory=True)
    result = _run(tmp_path, package, host, env, command)
    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    calls = _calls(tmp_path)
    assert {row["root"] for row in calls} == {str(old)}
    starts = [row for row in calls if row["action"] == "start"]
    assert [row["kind"] for row in starts] == (
        ["backend", "dashboard"] if running == "both" else ["dashboard"]
    )
    assert all(
        row["identity"] == "previous-claude"
        and row["selected_root"] == str(old)
        and row["marker"] == "prior marker"
        for row in starts
    )
    assert not candidate.exists()


@pytest.mark.parametrize("host", ["codex", "opencode"])
def test_no_previous_runtime_never_uses_unprepared_source_for_service_recovery(
    tmp_path, host
):
    package, root, _, env = _fixture(tmp_path, host, installed=False, running="both")
    result = _run(tmp_path, package, host, env)
    assert result.returncode != 0
    assert "uv sync failed" in result.stderr, result.stderr
    assert not _calls(tmp_path)
    assert not root.exists()


def test_missing_codex_package_file_fails_before_service_effects(tmp_path):
    package, root, _, env = _fixture(tmp_path, "codex")
    (package / "plugin/scripts/codex-hook.js").unlink()
    result = _run(tmp_path, package, "codex", env)
    assert result.returncode != 0
    assert "published package is missing" in result.stderr
    assert not _calls(tmp_path)
    assert (root / "identity").read_text() == "old"


@pytest.mark.parametrize("phase", ["registration", "bootstrap", "success", "uninstall"])
def test_codex_marketplace_never_replaces_legacy_claude_runtime(tmp_path, phase):
    package, root, uv, env = _fixture(
        tmp_path, "codex", installed=False, running="both"
    )
    legacy = tmp_path / ".claude/plugins/marketplaces/reflexioai/plugin"
    shutil.copytree(package / "plugin", legacy)
    (legacy / "identity").write_text("legacy-claude")
    (legacy / "prepared-only").write_text("prior environment and dashboard build")
    link = tmp_path / ".reflexio/plugin-root"
    link.parent.mkdir()
    link.symlink_to(legacy, target_is_directory=True)
    if phase != "bootstrap":
        _write_bootstrap_uv(uv, mode="fresh")
    if phase == "registration":
        fake = tmp_path / "fake-bin/codex"
        fake.write_text(
            fake.read_text().replace(
                'if (args[0] === "features"',
                'if (args[0] === "plugin" && args[1] === "marketplace" && args[2] === "add") process.exit(3);\nif (args[0] === "features"',
            )
        )
    before = {
        str(f.relative_to(legacy)): f.read_bytes()
        for f in legacy.rglob("*")
        if f.is_file()
    }
    result = _run(tmp_path, package, "codex", env)
    assert result.returncode == (
        0
        if phase in {"success", "uninstall"}
        else (3 if phase == "registration" else 1)
    ), result.stderr
    if phase == "uninstall":
        result = _run(tmp_path, package, "codex", env, "uninstall")
        assert result.returncode == 0, result.stderr
    after = {
        str(f.relative_to(legacy)): f.read_bytes()
        for f in legacy.rglob("*")
        if f.is_file()
    }
    assert after == before
    starts = [row for row in _calls(tmp_path) if row["action"] == "start"]
    if phase in {"registration", "bootstrap"}:
        assert len(starts) == 2
        assert all(
            row["root"] == str(legacy)
            and row["identity"] == "legacy-claude"
            and row["selected_root"] == str(legacy)
            for row in starts
        )
        assert not root.exists()
    else:
        assert not starts
