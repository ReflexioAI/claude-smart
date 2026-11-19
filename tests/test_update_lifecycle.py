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
    result = subprocess.run(
        [
            node_bin(),
            str(package / "bin/claude-smart.js"),
            "update",
            "--host",
            host,
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
        assert b"REFLEXIO_ROW_LIMIT_INTERACTIONS=731\n" in settings.read_bytes()
        assert b"CLAUDE_SMART_READ_ONLY=1\n" in settings.read_bytes()
        assert (root / "identity").read_text() == "new"
        assert adapter.exists()
        return
    if outcome in ("success", "ack-disconnects"):
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "Preparing Python and runtime dependencies" in result.stdout
        assert b"CLAUDE_SMART_READ_ONLY=1\n" in settings.read_bytes()
        assert (root / "identity").read_text() == "new"
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
            assert (root / "opencode/dist/server.mjs").as_uri() in adapter.read_text()
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
    log = next((tmp_path / ".claude-smart/install-logs").glob(f"update-{host}-*.log"))
    expected_error = {
        "stale-settings": "Settings changed after confirmation",
        "invalid-default": "Settings save could not be confirmed",
        "guide-disconnects": "Installation guide disconnected before settings publication",
        "early-disconnect": "Installation guide disconnected before passing settings",
        "preparation-fails": "uv sync failed",
    }[outcome]
    assert expected_error in log.read_text()


def _run_guided_node_update(home, package, env, host):
    env = {
        key: value for key, value in env.items() if key != "CLAUDE_SMART_INSTALL_CHILD"
    }
    return subprocess.run(
        [
            node_bin(),
            str(package / "bin/claude-smart.js"),
            "update",
            "--host",
            host,
            "--backend",
            "local",
            "--provider",
            host,
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


@pytest.mark.parametrize("stage", ["build", "publish"])
def test_guided_opencode_keeps_settings_when_loader_appears(tmp_path, stage):
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

    result = _run_guided_node_update(tmp_path, package, env, "opencode")
    assert result.returncode != 0
    assert settings.read_bytes() == original
    assert adapter.read_text() == "foreign loader\n"
    assert (root / "identity").read_text() == "old"
    log = next((tmp_path / ".claude-smart/install-logs").glob("update-opencode-*.log"))
    expected = {
        "build": "It is not a claude-smart loader",
        "publish": "OpenCode loader changed before settings were saved",
    }
    assert expected[stage] in log.read_text()


@pytest.mark.parametrize(
    "failure",
    [
        "create",
        "refresh",
        "publish",
        "partial-create",
        "partial-refresh",
        "close",
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
    preload = tmp_path / "loader-denied.cjs"
    preload.write_text(
        """
const fs = require("node:fs");
const { basename } = require("node:path");
const failure = """
        + json.dumps(failure)
        + """;
const open = fs.openSync;
let loaderFd;
let injected = false;
fs.openSync = (path, flags, ...args) => {
  if (typeof path === "string" &&
      ((failure === "create" && basename(path).startsWith(".claude-smart-loader-")) ||
       (failure === "refresh" && basename(path) === "claude-smart.js" &&
        (flags === "r+" || (typeof flags === "number" && flags & fs.constants.O_RDWR)))))
    throw Object.assign(new Error("fixture denied loader setup"), { code: "EACCES" });
  if (failure === "publish" && process.send && flags === "wx" &&
      typeof path === "string" && basename(path) === "claude-smart.js")
    throw Object.assign(new Error("fixture denied loader publication"), { code: "EACCES" });
  const fd = open(path, flags, ...args);
  if (typeof path === "string" && basename(path) === "claude-smart.js" &&
      (flags === "wx" || (typeof flags === "number" && flags & fs.constants.O_RDWR)))
    loaderFd = fd;
  return fd;
};
if (failure === "close") {
  const close = fs.closeSync;
  fs.closeSync = fd => {
    close(fd);
    if (fd === loaderFd) {
      loaderFd = undefined;
      throw new Error("fixture could not close the published loader");
    }
  };
}
if (failure.startsWith("partial-")) {
  const truncate = fs.ftruncateSync;
  fs.ftruncateSync = (fd, length) => {
    if (fd === loaderFd && !injected) {
      injected = true;
      throw new Error("fixture interrupted loader write");
    }
    return truncate(fd, length);
  };
  const close = fs.closeSync;
  fs.closeSync = fd => {
    close(fd);
    if (fd === loaderFd) loaderFd = undefined;
  };
  const remove = fs.rmSync;
  fs.rmSync = (path, ...args) => {
    if (loaderFd !== undefined && basename(path) === "claude-smart.js")
      throw new Error("fixture requires the loader handle to close before deletion");
    return remove(path, ...args);
  };
}
"""
    )
    env["NODE_OPTIONS"] = f'--require "{preload}"'

    result = _run_guided_node_update(tmp_path, package, env, "opencode")

    assert result.returncode != 0
    assert (root / "identity").read_text() == "old"
    assert list(directory.glob(".claude-smart-loader-*.tmp")) == []
    assert (loader.read_bytes() if loader.exists() else None) == previous_loader
    log = next((tmp_path / ".claude-smart/install-logs").glob("update-opencode-*.log"))
    if failure in {"publish", "partial-create", "partial-refresh", "close"}:
        assert b"CLAUDE_SMART_READ_ONLY=1\n" in settings.read_bytes()
        assert (
            "Account settings were saved. Retry setup to finish registration."
            in result.stderr
        )
        expected = {
            "publish": "fixture denied loader publication",
            "partial-create": "fixture interrupted loader write",
            "partial-refresh": "fixture interrupted loader write",
            "close": "fixture could not close the published loader",
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

    result = _run_guided_node_update(tmp_path, package, env, "codex")

    assert result.returncode != 0
    assert settings.read_bytes() == before
    assert (root / "identity").read_text() == "old"
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()
    log = next((tmp_path / ".claude-smart/install-logs").glob("update-codex-*.log"))
    assert "could not prepare claude-smart Codex runtime" in log.read_text()
    assert reason in log.read_text()


@pytest.mark.parametrize("stage_change", ["edit", "replace"])
def test_guided_opencode_publishes_generated_bytes_after_staging(
    tmp_path, stage_change
):
    package, root, uv, env = _fixture(tmp_path, "opencode", running="none")
    _write_bootstrap_uv(uv, mode="fresh")
    preload = tmp_path / "changed-stage.cjs"
    preload.write_text(
        """
const fs = require("node:fs");
const { join } = require("node:path");
if (process.send) {
  const send = process.send;
  process.send = (message, ...args) => {
    if (message === "settings-published") {
      const directory = join(process.env.HOME, ".opencode/plugins");
      const stage = fs.readdirSync(directory).find(name => name.startsWith(".claude-smart-loader-"));
      if (!stage) throw new Error("fixture could not locate the stage");
      const path = join(directory, stage);
      if (process.env.FIXTURE_STAGE_CHANGE === "replace") {
        const replacement = join(directory, ".fixture-replacement");
        fs.writeFileSync(replacement, "foreign bytes");
        fs.renameSync(replacement, path);
      } else fs.writeFileSync(path, "foreign bytes");
      fs.writeFileSync(join(process.env.HOME, "stage-altered"), "");
    }
    return send.call(process, message, ...args);
  };
}
"""
    )
    env["NODE_OPTIONS"] = f'--require "{preload}"'
    env["FIXTURE_STAGE_CHANGE"] = stage_change

    result = _run_guided_node_update(tmp_path, package, env, "opencode")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / "stage-altered").exists()
    directory = tmp_path / ".opencode/plugins"
    loader = (directory / "claude-smart.js").read_text()
    assert loader.endswith(
        f'export {{ default }} from "{(root / "opencode/dist/server.mjs").as_uri()}";\n'
    )
    staged = list(directory.glob(".claude-smart-loader-*.tmp"))
    if stage_change == "replace":
        assert [path.read_bytes() for path in staged] == [b"foreign bytes"]
    else:
        assert not staged


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
