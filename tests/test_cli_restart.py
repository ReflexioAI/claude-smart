"""Tests for ``claude_smart.cli.cmd_restart``.

The command orchestrates four moving parts (stop backend, stop dashboard,
optional npm build, start both) and has several early-exit branches —
covering the non-obvious ones so refactors can't silently regress them.
"""

from __future__ import annotations

import argparse
import subprocess
from typing import Any

import pytest

from claude_smart import cli


def _make_args(**overrides: Any) -> argparse.Namespace:
    """Build a Namespace matching the ``restart`` subparser defaults."""
    defaults = {"skip_backend": False, "skip_dashboard": False, "no_rebuild": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


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
        if argv[0] == "npm":
            build_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        # Service script invocation: ``[script, subcmd]``.
        name = argv[0].rsplit("/", 1)[-1]
        service_calls.append((name, argv[1]))
        # _service_status uses capture_output=True; return canned stdout.
        if capture_output:
            return subprocess.CompletedProcess(argv, 0, "running on http://x\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/npm")
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
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    rc = cli.cmd_restart(_make_args())
    assert rc == 0
    assert build_calls == []
    # stop backend, stop dashboard, start backend, start dashboard
    # (trailing status calls for the status-line are not part of the contract).
    lifecycle = [sub for _name, sub in service_calls if sub != "status"]
    assert lifecycle == ["stop", "stop", "start", "start"]
    err = capsys.readouterr().err
    assert "npm not on PATH" in err


def test_restart_starts_backend_even_when_dashboard_build_fails(
    fake_services, monkeypatch, capsys
) -> None:
    """Guards the recovery path in cmd_restart: build fail → backend still comes up."""
    service_calls, _ = fake_services

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        argv = [str(c) for c in cmd]
        if argv[0] == "npm":
            # Only the build step fails — install must succeed so we exercise
            # the build-failure recovery path, not the install-failure one.
            if argv[1:3] == ["run", "build"]:
                raise subprocess.CalledProcessError(2, argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        name = argv[0].rsplit("/", 1)[-1]
        service_calls.append((name, argv[1]))
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
    assert subs == ["stop", "start"]
    assert dash_subs == ["stop"]
    out = capsys.readouterr()
    assert "dashboard build failed" in out.err
    # The backend status line confirms the user sees the recovery happened.
    assert "reflexio backend:" in out.out


def test_restart_no_rebuild_flag_skips_npm(fake_services) -> None:
    _, build_calls = fake_services
    rc = cli.cmd_restart(_make_args(no_rebuild=True))
    assert rc == 0
    assert build_calls == []


def test_restart_uses_npm_ci_when_lockfile_exists(fake_services) -> None:
    _, build_calls = fake_services
    (cli._DASHBOARD_DIR / "package-lock.json").write_text("{}\n")
    rc = cli.cmd_restart(_make_args())
    assert rc == 0
    assert build_calls[0] == ["npm", "ci", "--no-audit", "--no-fund"]
    assert build_calls[1] == ["npm", "run", "build"]
