"""Real installer/update dispatch with owned fake services and runtime tooling."""

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_install_scripts import (
    REPO_ROOT,
    _copy_codex_install_sources,
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
    _copy_codex_install_sources(package)
    opencode_entry = package / "plugin/opencode/dist/server.mjs"
    opencode_entry.parent.mkdir(parents=True)
    opencode_entry.write_text("export default {};\n")
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
    stop_requested = os.environ.get("FIXTURE_STOP_REQUESTED")
    if stop_requested and kind == "backend":
        Path(stop_requested).touch()
        time.sleep(0.5)
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
    codex = fake / "codex"
    _write_fake_codex(codex)
    # The version probe uses the install worker's PATH, which includes the
    # private Node stub below. Run the CLI fixture with the real interpreter.
    codex.write_text(
        codex.read_text().replace("#!/usr/bin/env node", f"#!{node_bin()}", 1)
    )
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


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "preparation-fails",
        "stale-settings",
        "invalid-default",
        "guide-disconnects",
        "early-disconnect",
        "ack-disconnects",
        pytest.param(
            "guide-exits-after-save",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="POSIX uncatchable signal"
            ),
        ),
    ],
)
def test_guided_node_host_uses_prepared_interpreter_for_settings(
    tmp_path, host, outcome
):
    package, root, uv, env = _fixture(tmp_path, host, running="none")
    # Local saves belong to the prepared generation. Managed single-host saves
    # retain the deferred IPC worker, so its disconnect cases exercise that mode.
    managed = outcome in {
        "guide-disconnects",
        "early-disconnect",
        "ack-disconnects",
        "guide-exits-after-save",
    }
    env["FIXTURE_GUIDED_API_KEY"] = "owned-test-key"
    adapter = (
        tmp_path / ".codex/claude-smart/marketplace"
        if host == "codex"
        else tmp_path / ".opencode/plugins/claude-smart.js"
    )
    env.pop("CLAUDE_SMART_INSTALL_CHILD")
    env["CLAUDE_SMART_DEFER_SHARED_SERVICES"] = "1"
    env["REFLEXIO_ROW_LIMIT_INTERACTIONS"] = (
        "not\na single line" if outcome == "invalid-default" else "731"
    )
    settings = tmp_path / ".claude-smart/.env"
    before = settings.read_bytes()
    node = tmp_path / ".claude-smart/node/current/bin/node"
    _write_executable(node, f'#!/bin/sh\nexec {shlex.quote(node_bin())} "$@"\n')
    if outcome != "preparation-fails":
        _write_bootstrap_uv(uv, mode="fresh")
    uv_source = uv.read_text()
    change = ":"
    if outcome == "stale-settings":
        change = (
            'printf "# concurrent edit\n" >> "$HOME/.claude-smart/.env"; '
            'printf "other host diagnostic" > "$HOME/.claude-smart/install-failed"'
        )
    if outcome == "guide-disconnects":
        change = (
            "for attempt in 1 2 3 4 5 6 7 8 9 10; do "
            '[ ! -e "$HOME/guide-disconnected" ] || break; sleep 0.5; done; '
            '[ -e "$HOME/guide-disconnected" ] || exit 97'
        )
        preload = tmp_path / "disconnect-guide.cjs"
        preload.write_text("""
const {existsSync, writeFileSync} = require("node:fs");
const {join} = require("node:path");
const childProcess = require("node:child_process");
const spawn = childProcess.spawn;
if (typeof process.send === "function") {
  process.once("disconnect", () => writeFileSync(join(process.env.HOME, "guide-disconnected"), ""));
}
childProcess.spawn = function(command, args, options) {
  const child = spawn(command, args, options);
  if (options?.stdio?.includes?.("ipc")) {
    const timer = setInterval(() => {
      if (existsSync(join(process.env.HOME, "settings-at-sync"))) {
        child.disconnect();
        clearInterval(timer);
      }
    }, 10);
    child.once("exit", () => clearInterval(timer));
  }
  return child;
};
""")
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    if outcome == "early-disconnect":
        preload = tmp_path / "early-disconnect.mjs"
        preload.write_text("""
import {createRequire} from "node:module";
if (typeof process.send === "function") {
  if (process.connected) await new Promise(resolve => process.once("disconnect", resolve));
} else {
  const childProcess = createRequire(import.meta.url)("node:child_process");
  const spawn = childProcess.spawn;
  childProcess.spawn = function(command, args, options) {
    const child = spawn(command, args, options);
    if (options?.stdio?.includes?.("ipc")) {
      setTimeout(() => { if (child.connected) child.disconnect(); }, 50);
    }
    return child;
  };
}
""")
        env["NODE_OPTIONS"] = f'--import "{preload.as_uri()}"'
    if outcome == "ack-disconnects":
        preload = tmp_path / "disconnect-ack.cjs"
        preload.write_text("""
if (process.send) {
  const send = process.send;
  process.send = function(message, ...args) {
    if (message === "settings-published") process.disconnect();
    return send.call(this, message, ...args);
  };
}
""")
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    if outcome == "guide-exits-after-save":
        preload = tmp_path / "kill-guide.cjs"
        preload.write_text("""
const {copyFileSync, writeFileSync} = require("node:fs");
const {join} = require("node:path");
if (typeof process.send === "function") {
  const send = process.send;
  process.send = function(message, ...args) {
    if (message !== "settings-published") return send.call(this, message, ...args);
    const home = process.env.HOME;
    copyFileSync(join(home, ".claude-smart", ".env"), join(home, "settings-at-ack"));
    process.once("exit", (code) =>
      writeFileSync(join(home, "worker-exit"), String(code)));
    const guide = process.ppid;
    process.kill(guide, "SIGKILL");
    const deadline = Date.now() + 5000;
    const wait = new Int32Array(new SharedArrayBuffer(4));
    while (process.ppid === guide && Date.now() < deadline)
      Atomics.wait(wait, 0, 0, 10);
    if (process.ppid === guide) throw new Error("Fixture failed to kill guide");
  };
}
""")
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    _write_executable(
        uv,
        uv_source.replace(
            "#!/bin/sh\n",
            "#!/bin/sh\n"
            'if [ "$1" = sync ] && [ ! -e "$HOME/settings-at-sync" ]; then\n'
            '  cp "$HOME/.claude-smart/.env" "$HOME/settings-at-sync"\n'
            f"  {change}\n"
            "fi\n",
            1,
        ),
    )
    fetch_stub = tmp_path / "managed-fetch.cjs"
    fetch_stub.write_text("global.fetch=async()=>({ok:true,json:async()=>({})});\n")
    env["NODE_OPTIONS"] = (
        env.get("NODE_OPTIONS", "") + f' --require "{fetch_stub}"'
    ).strip()
    backend_args = (
        [
            "--backend",
            "managed",
            "--url",
            "https://managed.invalid/",
            "--api-key-env",
            "FIXTURE_GUIDED_API_KEY",
        ]
        if managed
        else ["--backend", "local", "--provider", "codex"]
    )
    result = subprocess.run(
        [
            node_bin(),
            str(package / "bin/claude-smart.js"),
            "update",
            "--host",
            host,
            *backend_args,
            "--read-only",
            "--yes",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    at_sync = tmp_path / "settings-at-sync"
    if outcome == "early-disconnect":
        assert not at_sync.exists()
        assert not (tmp_path / "uv.log").exists()
    else:
        assert at_sync.read_bytes() == before
        assert "uv venv" not in (tmp_path / "uv.log").read_text()
    if outcome == "guide-exits-after-save":
        assert result.returncode == -signal.SIGKILL, (result.stdout, result.stderr)
        worker_exit = tmp_path / "worker-exit"
        deadline = time.monotonic() + 10
        while (
            not worker_exit.exists() or not worker_exit.stat().st_size
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert worker_exit.read_text() == "0"
        assert (tmp_path / "settings-at-ack").read_bytes() == settings.read_bytes()
        assert b"REFLEXIO_URL=https://managed.invalid/" in settings.read_bytes()
        assert b"REFLEXIO_ROW_LIMIT_INTERACTIONS=" not in settings.read_bytes()
        assert b"CLAUDE_SMART_READ_ONLY=1\n" in settings.read_bytes()
        assert (root / "identity").read_text() == "new"
        assert adapter.exists()
        return
    if outcome in ("success", "ack-disconnects"):
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "Preparing Python and runtime dependencies" in result.stdout
        assert b"CLAUDE_SMART_READ_ONLY=1\n" in settings.read_bytes()
        if managed:
            active_root = root
        else:
            selector = tmp_path / ".claude-smart/guided-runtimes/selected"
            active_root = selector.parent / selector.read_text().strip() / "plugin"
            assert (root / "identity").read_text() == "old"
            assert (
                b"REFLEXIO_NATIVE_CLI_MODEL=local-cli/codex\n" in settings.read_bytes()
            )
            assert b"CLAUDE_SMART_LEARNING_PROVIDER=" not in settings.read_bytes()
            assert b"REFLEXIO_ROW_LIMIT_INTERACTIONS=731\n" in settings.read_bytes()
        assert (active_root / "identity").read_text() == "new"
        if host == "codex":
            manifest = next(adapter.rglob("codex-hooks.json"))
            contents = manifest.read_text()
            launchers = list(
                (tmp_path / ".claude-smart/node/launchers").glob("*/bin/node")
            )
            assert len(launchers) == 1
            assert str(launchers[0].resolve()) in contents
            assert str(node.resolve()) not in contents
        else:
            assert (
                active_root / "opencode/dist/server.mjs"
            ).as_uri() in adapter.read_text()
        return

    assert result.returncode != 0
    expected = (
        before + b"# concurrent edit\n" if outcome == "stale-settings" else before
    )
    assert settings.read_bytes() == expected
    if outcome == "stale-settings":
        assert (
            settings.parent / "install-failed"
        ).read_text() == "other host diagnostic"
    assert (root / "identity").read_text() == "old"
    assert not adapter.exists()
    # Local preparation fails in the guide before any registration child/log.
    report = result.stdout + result.stderr
    if managed:
        log = next(
            (tmp_path / ".claude-smart/install-logs").glob(f"update-{host}-*.log")
        )
        report += log.read_text()
    expected_error = {
        "stale-settings": "Settings changed after confirmation",
        "invalid-default": "Settings save could not be confirmed",
        "guide-disconnects": "Installation guide disconnected before settings publication",
        "early-disconnect": "Installation guide disconnected before passing settings",
        "preparation-fails": "uv sync failed",
    }[outcome]
    assert expected_error in report, report


@pytest.mark.parametrize(
    "failure",
    [
        "python",
        "opencode-loader",
        "opencode-entry",
        "codex-version",
        "redirected-scope",
        "redirected-backup",
    ],
)
def test_multihost_preparation_failure_preserves_settings_and_registration(
    tmp_path, failure
):
    package, old_root, uv, env = _fixture(tmp_path, "codex", running="none")
    env.pop("CLAUDE_SMART_INSTALL_CHILD")
    settings = tmp_path / ".claude-smart/.env"
    original = settings.read_bytes()
    loader = tmp_path / ".opencode/plugins/claude-smart.js"
    loader.parent.mkdir(parents=True)
    original_loader = (
        "// claude-smart OpenCode V2 loader\n"
        "// OpenCode V2 auto-loads this directory. Reinstall claude-smart to refresh.\n"
        'export { default } from "file:///older/runtime/server.mjs";\n'
    )
    loader.write_text(original_loader)
    expected = "uv sync failed"
    if failure == "opencode-loader":
        if os.name == "nt" or os.geteuid() == 0:
            pytest.skip("requires unprivileged POSIX file permissions")
        _write_bootstrap_uv(uv, mode="fresh")
        loader.chmod(0o400)
        expected = "Permission denied"
    elif failure == "opencode-entry":
        _write_bootstrap_uv(uv, mode="fresh")
        (package / "plugin/opencode/dist/server.mjs").unlink()
        expected = "server.mjs"
    elif failure == "codex-version":
        _write_bootstrap_uv(uv, mode="fresh")
        (package / "plugin/.codex-plugin/plugin.json").write_text(
            '{"name":"claude-smart"}'
        )
        expected = "missing version"
    outside = tmp_path / "unrelated-file"
    if failure in {"redirected-scope", "redirected-backup"}:
        _write_bootstrap_uv(uv, mode="fresh")
        try:
            if failure == "redirected-scope":
                scoped = tmp_path / ".opencode"
                moved = tmp_path / "unrelated-scope"
                scoped.rename(moved)
                scoped.symlink_to(moved, target_is_directory=True)
            else:
                config = tmp_path / "opencode.jsonc"
                config.write_text('{ "plugin": ["claude-smart"], /* keep */ }\n')
                outside.write_text("outside sentinel")
                (tmp_path / "opencode.jsonc.bak").symlink_to(outside)
        except OSError:
            pytest.skip("filesystem does not support this test's symlink")
        expected = "Could not snapshot the OpenCode scope"
    result = subprocess.run(
        [
            node_bin(),
            str(package / "bin/claude-smart.js"),
            "update",
            "--host",
            "codex,opencode",
            "--backend",
            "local",
            "--provider",
            "codex",
            "--read-only",
            "--yes",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert expected in result.stderr, result.stderr
    assert settings.read_bytes() == original
    if failure == "redirected-backup":
        assert outside.read_text() == "outside sentinel"
        assert config.read_text() == '{ "plugin": ["claude-smart"], /* keep */ }\n'

    assert loader.read_text() == original_loader
    assert (tmp_path / ".reflexio/plugin-root").resolve() == old_root.resolve()
    assert not list((tmp_path / ".claude-smart/guided-runtimes").glob("runtime-*"))
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()
    assert not (tmp_path / "service-calls.jsonl").exists()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "selection",
        "shutdown",
        "binding",
        pytest.param(
            "interrupted-after-save",
            marks=pytest.mark.skipif(os.name == "nt", reason="uses POSIX SIGKILL"),
        ),
        pytest.param(
            "interrupted-selection",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="uses POSIX SIGKILL to model a crash"
            ),
        ),
    ],
)
def test_multihost_cutover_observes_prior_services_before_settings(tmp_path, failure):
    package, old_root, uv, env = _fixture(tmp_path, "codex")
    env.pop("CLAUDE_SMART_INSTALL_CHILD")
    env["CLAUDE_SMART_DEFER_SHARED_SERVICES"] = "1"
    _write_bootstrap_uv(uv, mode="fresh")
    codex = tmp_path / "fake-bin/codex"
    codex.write_text(
        codex.read_text().replace("#!/usr/bin/env node", f"#!{node_bin()}", 1)
    )
    selected = tmp_path / ".reflexio/plugin-root"
    if failure == "selection":
        selected.unlink()
        shutil.copytree(old_root, selected)
    if failure == "shutdown":
        env["FIXTURE_UNSTOPPABLE"] = "backend"
    if failure == "interrupted-selection":
        preload = tmp_path / "interrupted-selection.cjs"
        preload.write_text(
            """
const fs = require("node:fs");
const { join } = require("node:path");
const selector = join(process.env.HOME, ".reflexio/plugin-root");
if (!process.send) {
  const rename = fs.renameSync;
  const remove = fs.rmSync;
  fs.renameSync = (from, to) => {
    if (to === selector) throw Object.assign(new Error("fixture simulates a junction collision"), { code: "EPERM" });
    return rename(from, to);
  };
  fs.rmSync = (path, options) => {
    const result = remove(path, options);
    if (path === selector) process.kill(process.pid, "SIGKILL");
    return result;
  };
}
"""
        )
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    elif failure == "interrupted-after-save":
        preload = tmp_path / "interrupted-after-save.cjs"
        preload.write_text(
            """
const cp = require("node:child_process");
const spawn = cp.spawn;
cp.spawn = (command, args, ...rest) => {
  if (!process.send && args.includes("--host") && args.includes("codex")) process.kill(process.pid, "SIGKILL");
  return spawn(command, args, ...rest);
};
"""
        )
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    elif failure == "binding":
        preload = tmp_path / "interrupted-binding.cjs"
        preload.write_text(
            """
const fs = require("node:fs");
const { basename } = require("node:path");
const rename = fs.renameSync;
fs.renameSync = (from, to) => {
  if (to.includes("opencode-registrations") && /^[0-9a-f]{64}$/.test(basename(to)))
    throw new Error("fixture refused final binding");
  return rename(from, to);
};
"""
        )
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    args = [
        node_bin(),
        str(package / "bin/claude-smart.js"),
        "update",
        "--host",
        "codex,opencode",
        "--backend",
        "local",
        "--provider",
        "codex",
        "--url",
        "http://localhost:9090/",
        "--yes",
    ]
    result = subprocess.run(
        args,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [
        json.loads(line)
        for line in (tmp_path / "service-calls.jsonl").read_text().splitlines()
    ]
    assert [row["action"] for row in calls[:2]] == ["status", "status"]
    assert all("http://localhost:8071/" in row["file"] for row in calls[:2])
    if failure == "selection":
        assert result.returncode == 1
        assert "runtime selection failed" in result.stderr
        assert selected.is_dir() and not selected.is_symlink()
        assert all(row["action"] == "status" for row in calls)
    elif failure in {"interrupted-selection", "interrupted-after-save"}:
        assert result.returncode == -signal.SIGKILL
        if failure == "interrupted-selection":
            assert not selected.is_symlink()
            text = selected.with_name("plugin-root.txt")
            assert (
                Path(text.read_text().strip()) / "scripts/backend-service.sh"
            ).is_file()
        else:
            assert selected.resolve() == old_root
            assert (
                "http://localhost:9090/"
                in (tmp_path / ".claude-smart/.env").read_text()
            )
        records = tmp_path / ".claude-smart/guided-runtimes"
        pending = records / "pending-shutdown.json"
        assert json.loads(pending.read_text())["root"] == str(old_root)
        # SIGKILL leaves the guide lock; simulate expiry of its existing lease.
        os.utime(records / ".install.lock", (1, 1))
        env.pop("NODE_OPTIONS")
        if failure == "interrupted-after-save":
            _write_bootstrap_uv(uv, mode="non_lock_failure")
            refused = subprocess.run(
                args,
                check=False,
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert refused.returncode == 1
            assert "uv sync failed" in refused.stderr
            assert all(row["action"] != "stop" for row in _calls(tmp_path))
            assert pending.exists()
            _write_bootstrap_uv(uv, mode="fresh")
        recovered = subprocess.run(
            args,
            check=False,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert not pending.exists()
        assert any(
            row["action"] == "stop" and row["root"] == str(old_root)
            for row in _calls(tmp_path)
        )
    elif failure == "binding":
        assert result.returncode == 1
        assert (tmp_path / ".opencode/plugins/claude-smart.js").is_file()
        registrations = (
            tmp_path / ".claude-smart/guided-runtimes/opencode-registrations"
        )
        assert len(list(registrations.glob("*.pending"))) == 1
        env.pop("NODE_OPTIONS")
        status = subprocess.run(
            [
                node_bin(),
                str(package / "bin/claude-smart.js"),
                "status",
                "--host",
                "opencode",
            ],
            check=False,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert status.returncode == 0, status.stderr
        assert (
            "Prepared runtime version:" in status.stdout
            and "not installed" not in status.stdout
        )
        scope_owner = package / "plugin/src/claude_smart/opencode_scope.py"
        launch = 'if __name__ == "__main__":'
        scope_owner.write_text(
            scope_owner.read_text().replace(
                launch,
                'if sys.argv[1:] == ["publish"]:\n    raise SystemExit(1)\n\n' + launch,
            )
        )
        again = subprocess.run(
            args,
            check=False,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert again.returncode == 1
        status = subprocess.run(
            [
                node_bin(),
                str(package / "bin/claude-smart.js"),
                "status",
                "--host",
                "opencode",
            ],
            check=False,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert status.returncode == 0, status.stderr
        assert (
            "Prepared runtime version:" in status.stdout
            and "not installed" not in status.stdout
        )
    elif failure == "shutdown":
        assert result.returncode == 1
        assert "Could not confirm shutdown" in result.stderr
        pending = tmp_path / ".claude-smart/guided-runtimes/pending-shutdown.json"
        assert json.loads(pending.read_text())["root"] == str(old_root)
        assert selected.resolve() != old_root
        env.pop("FIXTURE_UNSTOPPABLE")
        recovered = subprocess.run(
            args,
            check=False,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert not pending.exists()
        attempts = [
            json.loads(line)
            for line in (tmp_path / "service-calls.jsonl").read_text().splitlines()
        ][len(calls) :]
        assert any(
            row["action"] == "stop"
            and row["kind"] == "backend"
            and row["root"] == str(old_root)
            for row in attempts
        )
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        stops = [row for row in calls if row["action"] == "stop"]
        assert {row["kind"] for row in stops} == {"backend", "dashboard"}
        assert all(row["root"] == str(old_root) for row in stops)
        assert all(row["selected_root"] == str(selected.resolve()) for row in stops)


def _run_guided_node_update(home, package, env, host, *, backend="managed"):
    env = {
        key: value for key, value in env.items() if key != "CLAUDE_SMART_INSTALL_CHILD"
    }
    # This family protects the deferred settings worker retained for managed
    # single-host registration. Local mode uses the prepared generation owner.
    env["FIXTURE_GUIDED_API_KEY"] = "owned-test-key"
    fetch_stub = home / "managed-fetch.cjs"
    fetch_stub.write_text("global.fetch=async()=>({ok:true,json:async()=>({})});\n")
    env["NODE_OPTIONS"] = (
        env.get("NODE_OPTIONS", "") + f' --require "{fetch_stub}"'
    ).strip()
    backend_args = (
        [
            "--backend",
            "managed",
            "--url",
            "https://managed.invalid/",
            "--api-key-env",
            "FIXTURE_GUIDED_API_KEY",
        ]
        if backend == "managed"
        else ["--backend", "local", "--provider", host]
    )
    return subprocess.run(
        [
            node_bin(),
            str(package / "bin/claude-smart.js"),
            "update",
            "--host",
            host,
            *backend_args,
            "--read-only",
            "--yes",
        ],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("backend", ["local", "managed"])
@pytest.mark.parametrize("stage", ["build", "publish"])
def test_guided_opencode_keeps_settings_when_loader_appears(tmp_path, stage, backend):
    package, root, uv, env = _fixture(tmp_path, "opencode", running="none")
    settings = tmp_path / ".claude-smart/.env"
    original = settings.read_bytes()
    adapter = tmp_path / ".opencode/plugins/claude-smart.js"
    adapter.parent.mkdir(parents=True)
    _write_bootstrap_uv(uv, mode="fresh")
    if stage == "build":
        _write_executable(
            uv,
            uv.read_text().replace(
                "#!/bin/sh\n",
                "#!/bin/sh\n"
                'if [ "$1" = sync ]; then\n'
                '  printf "foreign loader\\n" > "$HOME/.opencode/plugins/claude-smart.js"\n'
                "fi\n",
                1,
            ),
        )
    else:
        owner = package / "plugin/src/claude_smart/env_config.py"
        sync = "os.fsync(target.fileno())"
        owner.write_text(
            owner.read_text().replace(
                sync,
                f"{sync}\n                    Path({str(adapter)!r}).write_bytes(b'foreign loader\\n')",
                1,
            )
        )

    result = _run_guided_node_update(
        tmp_path, package, env, "opencode", backend=backend
    )
    assert result.returncode != 0
    assert settings.read_bytes() == original
    assert adapter.read_text() == "foreign loader\n"
    assert (root / "identity").read_text() == "old"
    report = result.stdout + result.stderr
    for log in (tmp_path / ".claude-smart/install-logs").glob("update-opencode-*.log"):
        report += log.read_text()
    expected = {
        "build": "It is not a claude-smart loader",
        "publish": "OpenCode loader changed before settings were saved",
    }
    assert expected[stage] in report, report


@pytest.mark.parametrize(
    "failure",
    [
        "create",
        "refresh",
        "publish",
        "partial-create",
        "partial-refresh",
    ],
)
def test_guided_opencode_loader_stage_and_settings_order(tmp_path, failure):
    package, root, uv, env = _fixture(tmp_path, "opencode", running="none")
    settings = tmp_path / ".claude-smart/.env"
    original = settings.read_bytes()
    directory = tmp_path / ".opencode/plugins"
    directory.mkdir(parents=True)
    loader = directory / "claude-smart.js"
    if failure in {"refresh", "partial-refresh"}:
        loader.write_text(
            "// claude-smart OpenCode V2 loader\n"
            "// OpenCode V2 auto-loads this directory. Reinstall claude-smart to refresh.\n"
            'export { default } from "file:///older/runtime/server.mjs";\n'
        )
    previous_loader = loader.read_bytes() if loader.exists() else None
    _write_bootstrap_uv(uv, mode="fresh")
    original_config = '{ "plugin": ["claude-smart"], /* keep */ }\n'
    config = tmp_path / "opencode.jsonc"
    if failure.startswith("partial-"):
        config.write_text(original_config)
    scope_owner = package / "plugin/src/claude_smart/opencode_scope.py"
    launch = 'if __name__ == "__main__":'
    fault = f"""
failure = {failure!r}
original_open = open_scoped_file
original_write = _write_bytes
loader_handle = None
injected = False

def fault_open(path, lifetime, **options):
    action = sys.argv[1]
    writable = options.get("writable", False)
    if action == "check" and (
        (failure == "create" and path.name.startswith(".claude-smart-loader-"))
        or (failure == "refresh" and writable and path.name == "claude-smart.js")
    ):
        raise PermissionError("fixture denied loader setup")
    if action == "publish" and failure == "publish" and path.name == "claude-smart.js":
        raise PermissionError("fixture denied loader publication")
    opened = original_open(path, lifetime, **options)
    if action == "publish" and writable and path.name == "claude-smart.js":
        global loader_handle
        loader_handle = opened[0]
    return opened

def fault_write(file, data):
    global injected
    if failure.startswith("partial-") and file is loader_handle and not injected:
        injected = True
        original_write(file, data[:10])
        raise OSError("fixture interrupted loader write")
    original_write(file, data)

open_scoped_file = fault_open
_write_bytes = fault_write
"""
    scope_owner.write_text(
        scope_owner.read_text().replace(launch, fault + "\n" + launch)
    )

    result = _run_guided_node_update(tmp_path, package, env, "opencode")

    assert result.returncode != 0
    assert (root / "identity").read_text() == "old"
    assert list(directory.glob(".claude-smart-loader-*.tmp")) == []
    assert (loader.read_bytes() if loader.exists() else None) == previous_loader
    if failure.startswith("partial-"):
        assert config.read_text() == original_config
        assert not config.with_suffix(".jsonc.bak").exists()
    log = next((tmp_path / ".claude-smart/install-logs").glob("update-opencode-*.log"))
    if failure in {"publish", "partial-create", "partial-refresh"}:
        assert b"CLAUDE_SMART_READ_ONLY=1\n" in settings.read_bytes()
        assert (
            "Account settings were saved. Retry setup to finish registration."
            in result.stderr
        )
        expected = {
            "publish": "fixture denied loader publication",
            "partial-create": "fixture interrupted loader write",
            "partial-refresh": "fixture interrupted loader write",
        }[failure]
        assert expected in log.read_text()
    else:
        assert settings.read_bytes() == original
        assert "Account settings were saved" not in result.stderr
        assert "fixture denied loader setup" in log.read_text()


@pytest.mark.parametrize(
    ("manifest", "reason"),
    [
        ("{invalid JSON}", "JSON"),
        ("{}", "missing a command for SessionStart"),
        (
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "echo setup"}]}
                        ],
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": 'bash "hook_entry.sh" codex stop',
                                    }
                                ]
                            }
                        ],
                    }
                }
            ),
            "missing a command for UserPromptSubmit",
        ),
    ],
)
def test_guided_codex_keeps_settings_when_hook_manifest_is_invalid(
    tmp_path, manifest, reason
):
    package, root, uv, env = _fixture(tmp_path, "codex", running="none")
    settings = tmp_path / ".claude-smart/.env"
    before = settings.read_bytes()
    _write_bootstrap_uv(uv, mode="fresh")
    node = tmp_path / ".claude-smart/node/current/bin/node"
    _write_executable(node, f'#!/bin/sh\nexec {shlex.quote(node_bin())} "$@"\n')
    (package / "plugin/hooks/codex-hooks.json").write_text(manifest)

    result = _run_guided_node_update(tmp_path, package, env, "codex", backend="local")

    assert result.returncode != 0
    assert settings.read_bytes() == before
    assert (root / "identity").read_text() == "old"
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()
    report = result.stdout + result.stderr
    for log in (tmp_path / ".claude-smart/install-logs").glob("update-codex-*.log"):
        report += log.read_text()
    assert reason in report, report


@pytest.mark.parametrize(
    "change", ["appeared", "redirected-ancestor", "config-appeared", "config-changed"]
)
def test_guided_opencode_recovers_if_scope_changes_after_settings_save(
    tmp_path, change
):
    package, root, uv, env = _fixture(tmp_path, "opencode", running="none")
    _write_bootstrap_uv(uv, mode="fresh")
    project_loader = tmp_path / ".opencode/plugins/claude-smart.js"
    outside = tmp_path / "unrelated"
    (outside / "plugins").mkdir(parents=True)
    outside_loader = outside / "plugins/claude-smart.js"
    outside_loader.write_text("outside sentinel")
    config = tmp_path / "opencode.json"
    if change == "config-changed":
        config.write_text('{"theme":"system"}')
    preload = tmp_path / "change-scope.cjs"
    preload.write_text(
        """
const fs = require("node:fs");
const { join } = require("node:path");
if (process.send) {
  const send = process.send;
  process.send = (message, ...args) => {
    if (message === "settings-published") {
      const project = join(process.env.HOME, ".opencode");
      if (process.env.FIXTURE_SCOPE_CHANGE === "appeared") {
        fs.writeFileSync(join(project, "plugins", "claude-smart.js"), "foreign loader", { flag: "wx" });
      } else if (process.env.FIXTURE_SCOPE_CHANGE.startsWith("config-")) {
        fs.writeFileSync(join(process.env.HOME, "opencode.json"), '{"plugin":["claude-smart"]}');
      } else {
        fs.renameSync(project, join(process.env.HOME, "previous-scope"));
        fs.symlinkSync(join(process.env.HOME, "unrelated"), project, "junction");
      }
    }
    return send.call(process, message, ...args);
  };
}
"""
    )
    env["NODE_OPTIONS"] = f'--require "{preload}"'
    env["FIXTURE_SCOPE_CHANGE"] = change
    result = _run_guided_node_update(tmp_path, package, env, "opencode")
    assert result.returncode != 0
    assert (
        "Account settings were saved. Retry setup to finish registration."
        in result.stderr
    )
    assert (root / "identity").read_text() == "old"
    assert outside_loader.read_text() == "outside sentinel"
    if change == "appeared":
        assert project_loader.read_text() == "foreign loader"
    elif change == "redirected-ancestor":
        assert not (tmp_path / "previous-scope/plugins/claude-smart.js").exists()
    else:
        assert json.loads(config.read_text()) == {"plugin": ["claude-smart"]}
        assert not project_loader.exists()


@pytest.mark.parametrize("marker_change", ["owned", "unrelated", "replaced"])
def test_guided_opencode_retry_clears_only_its_previous_failure(
    tmp_path, marker_change
):
    package, root, uv, env = _fixture(tmp_path, "opencode", running="none")
    for directory in (root, package / "plugin"):
        shutil.copy2(
            REPO_ROOT / "plugin/scripts/_lib.sh", directory / "scripts/_lib.sh"
        )
    _write_bootstrap_uv(uv, mode="fresh")
    fingerprint = subprocess.check_output(
        [
            "bash",
            "-c",
            '. ./scripts/_lib.sh && root="$(pwd -P)" && claude_smart_install_fingerprint_hash "$root" "$root/scripts"',
        ],
        cwd=root,
        env={**env, "PATH": str(uv.parent) + os.pathsep + env["PATH"]},
        text=True,
    ).strip()
    assert len(fingerprint) == 64
    marker = tmp_path / ".claude-smart/install-failed"
    if marker_change == "unrelated":
        fingerprint = "1" * 64
    original = f"previous transient failure\nfingerprint={fingerprint}\n"
    marker.write_text(original)
    if marker_change == "replaced":
        _write_executable(
            uv,
            uv.read_text().replace(
                "#!/bin/sh\n",
                "#!/bin/sh\n"
                'if [ "$1" = sync ]; then\n'
                '  cp "$HOME/.claude-smart/install-failed" "$HOME/new-marker"\n'
                '  mv "$HOME/new-marker" "$HOME/.claude-smart/install-failed"\n'
                "fi\n",
                1,
            ),
        )
    result = _run_guided_node_update(tmp_path, package, env, "opencode")
    assert result.returncode == 0, (result.stderr, result.stdout)
    assert (root / "identity").read_text() == "new"
    if marker_change == "owned":
        assert not marker.exists()
    else:
        assert marker.read_text() == original


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
@pytest.mark.parametrize("phase", ["status", "stop", "tcp"])
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
    elif phase == "status":
        env["FIXTURE_INITIAL_PROBE_STARTED"] = str(started)
    else:
        env["FIXTURE_STOP_REQUESTED"] = str(started)
    with socket.socket() as listener:
        if phase == "stop":
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            env["FIXTURE_PORT"] = str(listener.getsockname()[1])
        with subprocess.Popen(
            [
                node_bin(),
                str(package / "bin/claude-smart.js"),
                "update",
                "--host",
                "codex",
            ],
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
                if phase == "stop":
                    time.sleep(0.15)
                    listener.close()
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
    assert actions == ([] if phase == "status" else ["stop", "start"])


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


@pytest.mark.parametrize(
    "host, read_only", [("codex", False), ("codex", True), ("opencode", False)]
)
def test_successful_update_keeps_new_runtime_without_rollback_restart(
    tmp_path, host, read_only
):
    package, root, uv, env = _fixture(tmp_path, host)
    _write_bootstrap_uv(uv, mode="fresh")
    if read_only:
        settings = tmp_path / ".claude-smart/.env"
        settings.write_text(
            settings.read_text().replace(
                'CLAUDE_SMART_READ_ONLY="0"', 'CLAUDE_SMART_READ_ONLY="1"'
            )
        )
    result = _run(tmp_path, package, host, env)
    assert result.returncode == 0, result.stderr
    assert (root / "identity").read_text() == "new"
    if host == "codex":
        manifest = Path("hooks/codex-hooks.json")
        source = package / "plugin" / manifest
        assert (root / manifest).read_bytes() == source.read_bytes()
        marketplace = tmp_path / ".codex/claude-smart/marketplace/plugin" / manifest
        cache = tmp_path / ".codex/plugins/cache/reflexioai/claude-smart"
        (cached,) = cache.glob("*/" + str(manifest))
        assert marketplace.read_bytes() == cached.read_bytes()
        assert "--runtime-root" in marketplace.read_text()
        assert str(root) in marketplace.read_text()
        hooks = json.loads(marketplace.read_bytes())["hooks"]
        assert ("Stop" in hooks) == (not read_only)
        assert "UserPromptSubmit" in hooks
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


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({"CODEX_FIXTURE_VERSION": "0.147.0"}, "Codex CLI 0.148 or newer is required"),
        (
            {"CLAUDE_SMART_SERVICE_SCRIPT_TIMEOUT_MS": "240000"},
            "Remove CLAUDE_SMART_SERVICE_SCRIPT_TIMEOUT_MS",
        ),
    ],
)
@pytest.mark.parametrize("guided", [False, True])
def test_codex_preflight_failure_preserves_installed_runtime(
    tmp_path, settings, error, guided
):
    package, root, _, env = _fixture(tmp_path, "codex")
    env.update(settings)
    if guided:
        env.pop("CLAUDE_SMART_INSTALL_CHILD")

    result = _run(tmp_path, package, "codex", env)

    assert result.returncode != 0
    assert error in result.stderr
    assert not _calls(tmp_path)
    assert (root / "identity").read_text() == "old"


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


@pytest.mark.parametrize(
    ("host", "predecessor_host", "failure"),
    [
        ("codex", "opencode", "trust"),
        ("codex", "codex", "trust"),
        ("opencode", "codex", "binding"),
        pytest.param(
            "codex",
            "opencode",
            "trust-interrupt",
            marks=pytest.mark.skipif(os.name == "nt", reason="uses POSIX SIGKILL"),
        ),
        pytest.param(
            "codex",
            "opencode",
            "disabled-interrupt",
            marks=pytest.mark.skipif(os.name == "nt", reason="uses POSIX SIGKILL"),
        ),
        pytest.param(
            "opencode",
            "codex",
            "interrupt",
            marks=pytest.mark.skipif(os.name == "nt", reason="uses POSIX SIGKILL"),
        ),
    ],
)
def test_single_host_retry_recovers_abandoned_multihost_selection(
    tmp_path, host, predecessor_host, failure
):
    package, predecessor, uv, env = _fixture(tmp_path, predecessor_host)
    _write_bootstrap_uv(uv, mode="fresh")
    env.pop("CLAUDE_SMART_INSTALL_CHILD")
    if failure == "trust":
        env["CLAUDE_SMART_BACKEND_AUTOSTART"] = "1"
    else:
        env["CLAUDE_SMART_DEFER_SHARED_SERVICES"] = "1"

    state = tmp_path / ".claude-smart/guided-runtimes"
    state.mkdir()
    receipt = state / "pending-shutdown.json"
    baseline = f"running on http://localhost:{env['FIXTURE_PORT']}"
    receipt.write_text(
        json.dumps(
            {
                "id": "runtime-AbCd12",
                "root": str(predecessor),
                "status": {"dashboard": baseline, "backend": baseline},
            }
        )
    )
    if failure == "trust":
        codex = tmp_path / "fake-bin/codex"
        source = codex.read_text()
        dispatch = "const args = process.argv.slice(2);"
        assert source.count(dispatch) == 1
        codex.write_text(
            source.replace(
                dispatch,
                dispatch + '\nif (args[0] === "app-server") process.exit(17);',
                1,
            )
        )
    elif failure in {"trust-interrupt", "disabled-interrupt"}:
        preload = tmp_path / "interrupt-before-codex-trust.cjs"
        preload.write_text("""
const cp = require("node:child_process");
const fs = require("node:fs"), path = require("node:path");
const spawn = cp.spawn;
cp.spawn = (command, args, ...options) => {
  if (args[0] === "app-server") {
    if (process.env.FIXTURE_DISABLE_CODEX) {
      const config = path.join(process.env.CODEX_HOME, "config.toml");
      fs.writeFileSync(config, fs.readFileSync(config, "utf8").replace("enabled = true", "enabled = false"));
    }
    process.kill(process.pid, "SIGKILL");
  }
  return spawn(command, args, ...options);
};
""")
        env["NODE_OPTIONS"] = f'--require "{preload}"'
        if failure == "disabled-interrupt":
            env["FIXTURE_DISABLE_CODEX"] = "1"
    elif failure == "binding":
        preload = tmp_path / "refuse-final-opencode-binding.cjs"
        preload.write_text("""
const fs = require("node:fs"), path = require("node:path");
const rename = fs.renameSync;
fs.renameSync = (source, target) => {
  if (target.includes("opencode-registrations") && /^[0-9a-f]{64}$/.test(path.basename(target)))
    throw new Error("fixture refused final binding");
  return rename(source, target);
};
""")
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    else:
        preload = tmp_path / "interrupt-before-opencode-registration.cjs"
        preload.write_text("""
const fs = require("node:fs");
const rename = fs.renameSync;
fs.renameSync = (source, target) => {
  const result = rename(source, target);
  if (process.send && target.includes("opencode-registrations") && target.endsWith(".pending"))
    process.kill(process.pid, "SIGKILL");
  return result;
};
""")
        env["NODE_OPTIONS"] = f'--require "{preload}"'
    update = [
        node_bin(),
        str(package / "bin/claude-smart.js"),
        "update",
        "--host",
        host,
        "--backend",
        "local",
        "--provider",
        "codex",
        "--yes",
    ]
    run_options = {
        "cwd": tmp_path,
        "env": env,
        "text": True,
        "capture_output": True,
        "timeout": 60,
    }
    result = subprocess.run(update, check=False, **run_options)
    assert result.returncode == 1, result.stdout + result.stderr
    selector = tmp_path / ".reflexio/plugin-root"
    if failure == "trust":
        assert selector.resolve().parent.parent == state
        assert not receipt.exists()
        logs = list((tmp_path / ".claude-smart/install-logs").glob("update-codex-*"))
        assert "review and trust" in logs[0].read_text()
        calls = [
            json.loads(line)
            for line in (tmp_path / "service-calls.jsonl").read_text().splitlines()
        ]
        assert any(
            row["kind"] == "backend"
            and row["action"] == "start"
            and Path(row["root"]) == selector.resolve()
            for row in calls
        )
        assert (predecessor / "identity").read_text() == "old"
    elif failure == "trust-interrupt":
        assert selector.resolve().parent.parent == state
        assert not receipt.exists()
        assert (tmp_path / "fixture-backend-stopped").exists()
    elif failure == "disabled-interrupt":
        assert selector.resolve() == predecessor
        assert receipt.exists()
        assert not (tmp_path / "fixture-backend-stopped").exists()
    elif failure == "binding":
        assert selector.resolve().parent.parent == state
        assert not receipt.exists()
        assert (tmp_path / ".opencode/plugins/claude-smart.js").is_file()
        assert len(list((state / "opencode-registrations").glob("*.pending"))) == 1
        assert (tmp_path / "fixture-backend-stopped").exists()
    else:
        assert selector.resolve() == predecessor
        pending = json.loads(receipt.read_text())
        assert pending["root"] == str(predecessor)
        assert pending["id"] != "runtime-AbCd12"
        assert not (tmp_path / "fixture-backend-stopped").exists()
        assert not (tmp_path / "fixture-dashboard-stopped").exists()
        env.pop("NODE_OPTIONS")
        env["CODEX_HOME"] = str(tmp_path / ".another-codex-home")
        retry = subprocess.run(update, check=False, **run_options)
        assert retry.returncode == 0, retry.stdout + retry.stderr
        assert selector.resolve().parent.parent == state
        assert not receipt.exists()


@pytest.mark.parametrize(
    ("codex_stays_enabled", "legacy_hook_repointed", "claude"),
    [
        (True, True, None),
        (True, False, None),
        (False, True, None),
        (False, False, None),
        (False, False, "worker"),
        (False, False, "fallback"),
        (False, False, "unknown"),
        (False, False, "no-fallback"),
    ],
)
def test_uninstall_checks_surviving_integrations(
    tmp_path, codex_stays_enabled, legacy_hook_repointed, claude
):
    package, _old, uv, env = _fixture(tmp_path, "codex", installed=False)
    _write_bootstrap_uv(uv, mode="fresh")
    env.pop("CLAUDE_SMART_INSTALL_CHILD")
    env["CLAUDE_SMART_DEFER_SHARED_SERVICES"] = "1"
    codex = tmp_path / "fake-bin/codex"
    codex.write_text(
        codex.read_text().replace("#!/usr/bin/env node", f"#!{node_bin()}", 1)
    )
    _write_executable(
        package / "plugin/scripts/cli.sh",
        '#!/bin/sh\nprintf "%s:%s\n" "$(dirname "$0")" "$*"\n',
    )
    launcher = [node_bin(), str(package / "bin/claude-smart.js")]
    run_options = {
        "cwd": tmp_path,
        "env": env,
        "text": True,
        "capture_output": True,
        "timeout": 60,
    }
    setup = subprocess.run(
        [
            *launcher,
            "setup",
            "--host",
            "codex,opencode",
            "--backend",
            "local",
            "--provider",
            "codex",
            "--yes",
        ],
        check=False,
        **run_options,
    )
    assert setup.returncode == 0, setup.stdout + setup.stderr
    selector = tmp_path / ".reflexio/plugin-root"
    saved = selector.resolve()
    record = saved.parents[1] / "selected"
    assert record.read_text() == saved.parent.name
    if legacy_hook_repointed:
        legacy = tmp_path / "old-hook-runtime"
        shutil.copytree(package / "plugin", legacy)
        selector.unlink()
        selector.symlink_to(legacy, target_is_directory=True)
    command = subprocess.run(
        ["bash", str(record.with_name("guided-command.sh")), "cli.sh", "show"],
        check=False,
        **run_options,
    )
    assert command.returncode == 0, command.stderr
    assert command.stdout.strip() == f"{saved / 'scripts'}:show"
    home = Path(env["CODEX_HOME"])
    if legacy_hook_repointed:
        newer = record.parent / "runtime-AbZ123"
        shutil.copytree(saved.parent, newer, symlinks=True)
        adapter = home / "claude-smart/marketplace/plugin/guided-runtime-id"
        adapter.write_text(newer.name)
    if not codex_stays_enabled:
        config = home / "config.toml"
        config.write_text(
            config.read_text().replace("enabled = true", "enabled = false")
        )
    env["CODEX_HOME"] = str(tmp_path / "empty-codex-home")
    registrations = json.dumps(
        [{"id": "claude-smart@reflexioai", "readFromFolder": str(saved)}]
    )
    surviving_inventory = f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(registrations)}\n"
    failed_inventory = "#!/bin/sh\nexit 1\n"
    if claude == "worker":
        _write_executable(
            tmp_path / ".local/bin/claude",
            "#!/bin/sh\nexec claude-test-inventory\n",
        )
        _write_executable(
            tmp_path / ".local/bin/claude-test-inventory", surviving_inventory
        )
    elif claude is not None:
        _write_executable(tmp_path / ".local/bin/claude", failed_inventory)
    caller_inventory = {
        "fallback": surviving_inventory,
        "unknown": failed_inventory,
    }.get(claude or "", "#!/bin/sh\nprintf '%s\\n' '[]'\n")
    if claude == "no-fallback":
        env["PATH"] = os.pathsep.join(
            directory
            for directory in env["PATH"].split(os.pathsep)
            if directory and shutil.which("claude", path=directory) is None
        )
        assert shutil.which("claude", path=env["PATH"]) is None
    else:
        _write_executable(tmp_path / "fake-bin/claude", caller_inventory)
    removed = subprocess.run(
        [*launcher, "uninstall", "--host", "opencode"], check=False, **run_options
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not (tmp_path / ".opencode/plugins/claude-smart.js").exists()
    if claude is not None:
        assert selector.resolve() == saved
        assert record.read_text() == saved.parent.name
        if claude in {"unknown", "no-fallback"}:
            assert "Claude Code integrations could not be verified" in removed.stdout
        else:
            assert "Refreshing shared services" in removed.stdout
        return
    if codex_stays_enabled:
        if not legacy_hook_repointed:
            assert selector.resolve() == saved
        assert record.read_text() == saved.parent.name
        assert "Refreshing shared services" in removed.stdout
        env["CODEX_HOME"] = str(home)
        cleared = subprocess.run(
            [*launcher, "uninstall", "--host", "codex"], check=False, **run_options
        )
        assert cleared.returncode == 0, cleared.stdout + cleared.stderr
    assert not selector.exists()
    assert not record.exists()


@pytest.mark.parametrize("recorded", [False, True])
def test_guided_uninstall_keeps_standalone_codex_home(tmp_path, recorded):
    package, _old, uv, env = _fixture(tmp_path, "codex", installed=False)
    _write_bootstrap_uv(uv, mode="fresh")
    env.pop("CLAUDE_SMART_INSTALL_CHILD")
    env["CLAUDE_SMART_DEFER_SHARED_SERVICES"] = "1"
    codex = tmp_path / "fake-bin/codex"
    codex.write_text(
        codex.read_text().replace("#!/usr/bin/env node", f"#!{node_bin()}", 1)
    )
    launcher = [node_bin(), str(package / "bin/claude-smart.js")]
    standalone_home = Path(env["CODEX_HOME"])
    guided_home = tmp_path / "guided-codex-home"

    def run(*args, codex_home, standalone=False):
        result = subprocess.run(
            [*launcher, *args],
            cwd=tmp_path,
            env={
                **env,
                "CODEX_HOME": str(codex_home),
                **({"CLAUDE_SMART_INSTALL_CHILD": "1"} if standalone else {}),
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    setup = ["setup", "--backend", "local", "--provider", "codex", "--yes"]
    # Seed the retained standalone worker layout, then qualify its upgrade to
    # the public prepared coordinator. Public local setup always creates a generation.
    run("install", "--host", "codex", codex_home=standalone_home, standalone=True)
    if not recorded:
        registry = tmp_path / ".claude-smart/guided-runtimes/codex-homes"
        for entry in registry.iterdir():
            entry.unlink()
    run(*setup, "--host", "codex,opencode", codex_home=guided_home)
    if not recorded:
        # Simulate a later guided update interrupted after replacing the adapter
        # but before replacing the old default home's enabled standalone cache.
        binding = Path("claude-smart/marketplace/plugin/guided-runtime-id")
        shutil.copy2(guided_home / binding, standalone_home / binding)

    selector = tmp_path / ".reflexio/plugin-root"
    guided_root = selector.resolve()
    assert "guided-runtimes" in str(guided_root)
    run("uninstall", "--host", "codex", codex_home=guided_home)
    assert selector.resolve() == guided_root

    # A project deletion leaves its account record behind. Survivor discovery
    # must still select the verified standalone registration.
    loader = tmp_path / ".opencode/plugins/claude-smart.js"
    loader.unlink()
    observed = subprocess.run(
        [
            node_bin(),
            "-e",
            "console.log(JSON.stringify(require(process.argv[1]).installedPluginRoots()))",
            launcher[1],
        ],
        cwd=tmp_path,
        env={**env, "CODEX_HOME": str(guided_home)},
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    survivors = json.loads(observed.stdout)
    assert str(guided_root) not in survivors
    assert str(standalone_home / "claude-smart/runtime/plugin") in survivors
    result = run("uninstall", "--host", "opencode", codex_home=guided_home)
    assert "Refreshing shared services" in result
    assert selector.resolve() == standalone_home / "claude-smart/runtime/plugin"


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX SIGKILL")
@pytest.mark.parametrize("installed", [False, True])
def test_retry_starts_selected_runtime_before_preparing_replacement(
    tmp_path, installed
):
    package, predecessor, uv, env = _fixture(tmp_path, "codex", installed=installed)
    _write_bootstrap_uv(uv, mode="fresh")
    env.pop("CLAUDE_SMART_INSTALL_CHILD")
    codex = tmp_path / "fake-bin/codex"
    codex.write_text(
        codex.read_text().replace("#!/usr/bin/env node", f"#!{node_bin()}", 1)
    )
    preload = tmp_path / "interrupt-after-runtime-selection.cjs"
    preload.write_text("""
const fs = require("node:fs"), path = require("node:path");
const rename = fs.renameSync;
fs.renameSync = (source, target) => {
  const result = rename(source, target);
  if (target === path.join(process.env.HOME, ".claude-smart/guided-runtimes/selected"))
    process.kill(process.pid, "SIGKILL");
  return result;
};
""")
    env["NODE_OPTIONS"] = f'--require "{preload}"'
    env["CLAUDE_SMART_BACKEND_AUTOSTART"] = "1"
    env["BACKEND_PORT"] = env["FIXTURE_PORT"]
    command = [
        node_bin(),
        str(package / "bin/claude-smart.js"),
        "setup",
        "--host",
        "codex,opencode",
        "--backend",
        "local",
        "--url",
        f"http://localhost:{env['FIXTURE_PORT']}/",
        "--provider",
        "codex",
        "--yes",
    ]

    def run():
        return subprocess.run(
            command,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    interrupted = run()
    assert interrupted.returncode == -signal.SIGKILL, interrupted.stderr
    state = tmp_path / ".claude-smart/guided-runtimes"
    selected = tmp_path / ".reflexio/plugin-root"
    chosen = selected.resolve()
    receipt = state / "pending-shutdown.json"
    assert json.loads(receipt.read_text())["root"] == (
        str(predecessor) if installed else None
    )
    if installed:
        assert all(row["action"] != "stop" for row in _calls(tmp_path))

    os.utime(state / ".install.lock", (1, 1))
    env.pop("NODE_OPTIONS")
    _write_bootstrap_uv(uv, mode="non_lock_failure")
    failed_retry = run()
    assert failed_retry.returncode == 1, failed_retry.stdout + failed_retry.stderr
    assert "uv sync failed" in failed_retry.stderr
    assert selected.resolve() == chosen
    assert not receipt.exists()
    startup = state / "pending-startup"
    assert startup.read_text() == chosen.parent.name
    events = [(row["root"], row["action"]) for row in _calls(tmp_path)]
    started = events.index((str(chosen), "start"))
    if installed:
        assert events.index((str(predecessor), "stop")) < started

    # Repeated dependency failures must not discard the selected runtime's
    # unverified startup. Retry it again before touching the candidate.
    failed_again = run()
    assert failed_again.returncode == 1
    assert "uv sync failed" in failed_again.stderr
    assert startup.read_text() == chosen.parent.name
    assert [
        row["action"] for row in _calls(tmp_path) if row["root"] == str(chosen)
    ].count("start") == 2
