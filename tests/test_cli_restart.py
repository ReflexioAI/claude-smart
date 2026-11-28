"""Tests for ``claude_smart.cli.cmd_restart``.

The command orchestrates four moving parts (stop backend, stop dashboard,
optional npm build, start both) and has several early-exit branches —
covering the non-obvious ones so refactors can't silently regress them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from claude_smart import cli


def _make_args(**overrides: Any) -> argparse.Namespace:
    """Build a Namespace matching the ``restart`` subparser defaults."""
    defaults = {"skip_backend": False, "skip_dashboard": False, "no_rebuild": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_resolve_bash_non_windows_uses_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")

    assert cli._resolve_bash() == "/bin/bash"


def test_resolve_npm_non_windows_uses_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert cli._resolve_npm() == "/usr/bin/npm"


def test_resolve_bash_windows_prefers_env_bash(monkeypatch, tmp_path) -> None:
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_text("")
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setenv("BASH", str(bash))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli._resolve_bash() == str(bash)


def test_resolve_bash_windows_skips_system32_bash(monkeypatch, tmp_path) -> None:
    system32 = tmp_path / "Windows" / "System32"
    git = tmp_path / "Git" / "bin"
    system32.mkdir(parents=True)
    git.mkdir(parents=True)
    system_bash = system32 / "bash.exe"
    git_bash = git / "bash.exe"
    system_bash.write_text("")
    git_bash.write_text("")
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.delenv("BASH", raising=False)
    monkeypatch.setenv("PATH", f"{system32}{cli.os.pathsep}{git}")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        cli,
        "_resolve_candidate",
        lambda path: path if path in {str(system_bash), str(git_bash)} else None,
    )

    assert cli._resolve_bash() == str(git_bash)


def test_resolve_npm_windows_prefers_cmd_shim(monkeypatch) -> None:
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        calls.append(name)
        return "C:/Program Files/nodejs/npm.cmd" if name == "npm.cmd" else None

    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli.shutil, "which", fake_which)

    assert cli._resolve_npm() == "C:/Program Files/nodejs/npm.cmd"
    assert calls == ["npm.cmd"]


@pytest.fixture
def fake_services(monkeypatch, tmp_path):
    """Stub the service scripts as existing on disk and track invocations.

    Returns:
        tuple[list[tuple[str, str]], list[list[str]]]:
            ``(service_calls, build_calls)`` — service_calls holds
            ``(script_name, subcmd)`` pairs in call order; build_calls holds
            ``npm run build`` argv invocations.
    """
    # Point the module-level paths at tmp files so ``script.exists()`` passes.
    backend = tmp_path / "backend-service.sh"
    dashboard = tmp_path / "dashboard-service.sh"
    dash_dir = tmp_path / "dashboard"
    backend.write_text("#!/bin/sh\n")
    dashboard.write_text("#!/bin/sh\n")
    dash_dir.mkdir()
    monkeypatch.setattr(cli, "_BACKEND_SCRIPT", backend)
    monkeypatch.setattr(cli, "_DASHBOARD_SCRIPT", dashboard)
    monkeypatch.setattr(cli, "_DASHBOARD_DIR", dash_dir)

    service_calls: list[tuple[str, str]] = []
    build_calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        argv = [str(c) for c in cmd]
        if argv[0] == "/usr/bin/npm":
            build_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        # Service script invocation: ``[bash, script, subcmd]``.
        assert argv[0] == "/bin/bash"
        name = argv[1].rsplit("/", 1)[-1]
        service_calls.append((name, argv[2]))
        # _service_status uses capture_output=True; return canned stdout.
        if capture_output:
            return subprocess.CompletedProcess(argv, 0, "running on http://x\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_resolve_bash", lambda: "/bin/bash")
    monkeypatch.setattr(cli, "_resolve_npm", lambda: "/usr/bin/npm")
    return service_calls, build_calls


def test_restart_skip_flags_both_set_is_noop(fake_services, capsys) -> None:
    service_calls, build_calls = fake_services
    rc = cli.cmd_restart(_make_args(skip_backend=True, skip_dashboard=True))
    assert rc == 0
    assert service_calls == []
    assert build_calls == []
    assert "Nothing to restart" in capsys.readouterr().out


def test_restart_skips_rebuild_when_npm_missing(
    fake_services, monkeypatch, capsys
) -> None:
    """Missing npm → log warning, skip build, still stop/start both services."""
    service_calls, build_calls = fake_services
    monkeypatch.setattr(cli, "_resolve_npm", lambda: None)
    rc = cli.cmd_restart(_make_args())
    assert rc == 0
    assert build_calls == []
    # stop backend, stop dashboard, start backend, start dashboard
    # (trailing status calls for the status-line are not part of the contract).
    lifecycle = [sub for _name, sub in service_calls if sub != "status"]
    assert lifecycle == ["preflight", "stop", "stop", "start", "start"]
    err = capsys.readouterr().err
    assert "npm not on PATH" in err


def test_restart_starts_backend_even_when_dashboard_build_fails(
    fake_services, monkeypatch, capsys
) -> None:
    """Guards the recovery path in cmd_restart: build fail → backend still comes up."""
    service_calls, _ = fake_services

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        argv = [str(c) for c in cmd]
        if argv[0] == "/usr/bin/npm":
            # Only the build step fails — install must succeed so we exercise
            # the build-failure recovery path, not the install-failure one.
            if argv[1:3] == ["run", "build"]:
                raise subprocess.CalledProcessError(2, argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[0] == "/bin/bash"
        name = argv[1].rsplit("/", 1)[-1]
        service_calls.append((name, argv[2]))
        if capture_output:
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.cmd_restart(_make_args())
    assert rc == 2
    # Backend must be started despite the build failure; dashboard start is
    # intentionally skipped when the build can't produce a bundle.
    # (trailing status calls for the status-line are not part of the contract).
    subs = [
        sub
        for name, sub in service_calls
        if name == "backend-service.sh" and sub != "status"
    ]
    dash_subs = [
        sub
        for name, sub in service_calls
        if name == "dashboard-service.sh" and sub != "status"
    ]
    assert subs == ["preflight", "stop", "start"]
    assert dash_subs == ["stop"]
    out = capsys.readouterr()
    assert "dashboard build failed" in out.err
    # The backend status line confirms the user sees the recovery happened.
    assert "reflexio backend:" in out.out


def test_restart_starts_backend_when_dashboard_build_launch_fails(
    fake_services, monkeypatch, capsys
) -> None:
    """OSError during npm launch follows the same recovery path as build failure."""
    service_calls, _ = fake_services

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        argv = [str(c) for c in cmd]
        if argv[0] == "/usr/bin/npm":
            if argv[1:3] == ["run", "build"]:
                raise OSError("npm is not executable")
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[0] == "/bin/bash"
        name = argv[1].rsplit("/", 1)[-1]
        service_calls.append((name, argv[2]))
        if capture_output:
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.cmd_restart(_make_args())

    assert rc == 1
    subs = [
        sub
        for name, sub in service_calls
        if name == "backend-service.sh" and sub != "status"
    ]
    dash_subs = [
        sub
        for name, sub in service_calls
        if name == "dashboard-service.sh" and sub != "status"
    ]
    assert subs == ["preflight", "stop", "start"]
    assert dash_subs == ["stop"]
    out = capsys.readouterr()
    assert "dashboard build failed" in out.err
    assert "reflexio backend:" in out.out


def test_restart_no_rebuild_flag_skips_npm(fake_services) -> None:
    _, build_calls = fake_services
    rc = cli.cmd_restart(_make_args(no_rebuild=True))
    assert rc == 0
    assert build_calls == []


@pytest.mark.parametrize("launchable", [False, True])
def test_prepared_restart_preflights_and_reuses_the_dashboard(
    tmp_path, monkeypatch, fake_services, launchable
) -> None:
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", tmp_path)
    (tmp_path / "guide-prepared-runtime").write_text("runtime-AbCd12")
    service_calls, build_calls = fake_services
    original = cli._run_service

    def run_service(script, action):
        if (script, action) == (cli._DASHBOARD_SCRIPT, "preflight"):
            service_calls.append(("dashboard-service.sh", "preflight"))
            return 0 if launchable else 1
        return original(script, action)

    monkeypatch.setattr(cli, "_run_service", run_service)
    result = cli.cmd_restart(_make_args())
    assert build_calls == []
    assert service_calls[0] == ("dashboard-service.sh", "preflight")
    if launchable:
        assert result == 0
        assert ("dashboard-service.sh", "start") in service_calls
    else:
        assert result == 1
        assert len(service_calls) == 1


def test_restart_failed_backend_preflight_preserves_services(
    fake_services, monkeypatch, capsys
) -> None:
    service_calls, build_calls = fake_services

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        argv = [str(c) for c in cmd]
        if argv[-1] == "preflight":
            service_calls.append(("backend-service.sh", "preflight"))
            raise subprocess.CalledProcessError(1, argv)
        raise AssertionError(f"unexpected command after failed preflight: {argv}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.cmd_restart(_make_args())

    assert rc == 1
    assert service_calls == [("backend-service.sh", "preflight")]
    assert build_calls == []
    output = capsys.readouterr()
    assert "existing services were not changed" in output.err
    assert "Stopping" not in output.out


def test_restart_uses_npm_ci_when_lockfile_exists(fake_services) -> None:
    _, build_calls = fake_services
    (cli._DASHBOARD_DIR / "package-lock.json").write_text("{}\n")
    rc = cli.cmd_restart(_make_args())
    assert rc == 0
    assert build_calls[0] == [
        "/usr/bin/npm",
        "ci",
        "--omit=dev",
        "--no-audit",
        "--no-fund",
    ]
    assert build_calls[1] == ["/usr/bin/npm", "run", "build"]


@pytest.mark.parametrize(
    ("configured", "executable_name", "direct", "worker_path"),
    [
        ("", "opencode.exe", False, False),
        ("opencode.exe", "opencode.exe", False, False),
        ("opencode.cmd", "opencode.cmd", False, False),
        ("opencode", "opencode.exe", False, False),
        ("opencode", "opencode.exe", True, False),
        ("opencode", "opencode.cmd", False, False),
        ("opencode", "opencode.exe", False, True),
        ("opencode.exe", "opencode.exe", True, False),
        ("./opencode", "opencode.exe", True, False),
    ],
)
def test_windows_installer_resolves_configured_opencode_name(
    tmp_path, configured, executable_name, direct, worker_path
):
    node = shutil.which("node")
    assert node is not None
    installer = Path(__file__).resolve().parents[1] / "bin/claude-smart.js"
    search_dir = tmp_path / "tools with spaces"
    search_dir.mkdir()
    fallback = search_dir / executable_name
    if not (direct and configured == "opencode"):
        fallback.touch(mode=0o755)
    executable = tmp_path / executable_name if direct else fallback
    executable.touch(mode=0o755)
    if configured == "opencode":
        # Neither local spelling wins over PATH for an extensionless command.
        (tmp_path / "opencode.exe").touch(mode=0o755)
        if not direct:
            (tmp_path / "opencode").touch(mode=0o755)
    result = subprocess.run(
        [
            node,
            "-e",
            "console.log(require(process.argv[1]).resolveOpenCodePath(process.argv[2], process.argv[3]))",
            str(installer),
            search_dir.name if worker_path else "",
            configured,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "CLAUDE_SMART_TEST_PLATFORM": "win32",
            "PATH": "" if worker_path else search_dir.name,
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == executable.resolve()


@pytest.mark.parametrize("on_path", [True, False])
def test_windows_saved_opencode_command_ignores_the_project_copy(tmp_path, on_path):
    node = shutil.which("node")
    assert node is not None
    installer = Path(__file__).resolve().parents[1] / "bin/claude-smart.js"
    tools = tmp_path / "actual tools"
    tools.mkdir()
    filename = "opencode.cmd"
    (tmp_path / filename).touch(mode=0o755)
    expected = tools / filename
    if on_path:
        expected.touch(mode=0o755)
    result = subprocess.check_output(
        [
            node,
            "-e",
            "console.log(require(process.argv[1]).resolveOpenCodePath('', 'opencode.cmd', true))",
            str(installer),
        ],
        cwd=tmp_path,
        env={**os.environ, "CLAUDE_SMART_TEST_PLATFORM": "win32", "PATH": str(tools)},
        text=True,
        timeout=10,
    ).strip()
    if on_path:
        assert Path(result) == expected
    else:
        assert result == "null"


@pytest.mark.skipif(os.name != "nt", reason="requires the native Windows shell")
@pytest.mark.parametrize(
    "configured", [r"tools\opencode.cmd", r"tools\opencode", "opencode"]
)
def test_windows_opencode_batch_launcher_quotes_resolved_path(
    tmp_path, monkeypatch, configured
):
    node = shutil.which("node")
    assert node is not None
    bridge = (
        Path(__file__).resolve().parents[1] / "plugin/scripts/opencode-claude-compat.js"
    )
    working_dir = tmp_path / "runtime with spaces"
    executable = working_dir / "tools/opencode.cmd"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        '@echo {"type":"text","part":{"type":"text","text":"batch launched"}}\n'
    )
    if configured == "opencode":
        monkeypatch.setenv("PATH", "tools", prepend=os.pathsep)
    binding = subprocess.check_output(
        [
            node,
            "-e",
            "console.log(require(process.argv[1]).resolveWindowsOpenCodePath(process.argv[2]))",
            str(bridge),
            configured,
        ],
        cwd=working_dir,
        text=True,
        timeout=10,
    ).strip()
    assert Path(binding) == executable
    monkeypatch.setenv("_CLAUDE_SMART_NATIVE_OPENCODE_EXECUTABLE", binding)
    for name in (
        "CLAUDE_SMART_OPENCODE_MODEL",
        "CLAUDE_SMART_OPENCODE_VARIANT",
        "CLAUDE_SMART_LEARNING_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    result = subprocess.run(
        [node, str(bridge), "-p"],
        input="fixture prompt",
        text=True,
        capture_output=True,
        cwd=working_dir,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"result": "batch launched"}
