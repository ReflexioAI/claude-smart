"""A failed shutdown cannot authorize replacing a running installation."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.test_install_scripts import (
    _fake_claude_code_package,
    _run_fake_claude_code_install,
    _write_executable,
)
from tests.test_update_lifecycle import _fixture, _run


def _services(root: Path, failed: str, *, signaled: bool = False) -> None:
    for kind in ("backend", "dashboard"):
        _write_executable(
            root / f"scripts/{kind}-service.sh",
            '#!/bin/sh\ncase "$1" in\n'
            f'status) case "$FIXTURE_RUNNING" in both|{kind}) echo "running on http://localhost:1";; *) echo "not running";; esac;;\n'
            f'stop) echo "{kind}:stop" >> "$HOME/admission-calls"; '
            + (
                ("kill -TERM $$" if signaled else "exit 23")
                if failed in ("both", kind)
                else "exit 0"
            )
            + ";;\n"
            f'start) echo "{kind}:start" >> "$HOME/admission-calls";;\nesac\n',
        )


def _assert_rejected(
    home: Path,
    root: Path,
    result: CompletedProcess[str],
    failed: str,
    running: str,
) -> None:
    assert result.returncode != 0
    assert "Could not stop the previous" in result.stderr, result.stderr
    assert "runtime was not replaced" in result.stderr
    calls = (home / "admission-calls").read_text().splitlines()
    assert calls[:2] == ["dashboard:stop", "backend:stop"]
    restarted = [
        kind
        for kind in ("backend", "dashboard")
        if failed not in ("both", kind) and running in ("both", kind)
    ]
    assert calls[2:] == [f"{kind}:start" for kind in restarted]
    assert (root / "identity").read_text() == "old"
    assert (home / ".reflexio/plugin-root").resolve() == root
    assert not (home / "uv.log").exists()
    assert not (home / "prepared-after-failed-stop").exists()
    assert not list(root.parent.parent.glob(".claude-smart-previous*"))


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("command", ["install", "update"])
@pytest.mark.parametrize("failed", ["backend", "dashboard", "both"])
@pytest.mark.parametrize("running", ["both", "none"])
def test_failed_stop_blocks_replacement_for_native_host(
    tmp_path: Path, host: str, command: str, failed: str, running: str
) -> None:
    package, root, _, env = _fixture(tmp_path, host, running=running)
    _services(root, failed)
    settings = tmp_path / ".claude-smart/.env"
    original = settings.read_bytes()
    result = _run(tmp_path, package, host, env, command)
    _assert_rejected(tmp_path, root, result, failed, running)
    assert settings.read_bytes() == original
    assert (tmp_path / ".claude-smart/install-failed").read_text() == "prior marker"
    assert not (tmp_path / ".codex/claude-smart/marketplace").exists()


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("failed", ["backend", "dashboard"])
def test_signaled_stop_blocks_replacement(
    tmp_path: Path, host: str, failed: str
) -> None:
    package, root, _, env = _fixture(tmp_path, host)
    _services(root, failed, signaled=True)
    result = _run(tmp_path, package, host, env)
    _assert_rejected(tmp_path, root, result, failed, "both")
    assert "terminated by SIGTERM" in result.stderr


@pytest.mark.parametrize("failed", ["backend", "dashboard", "both"])
def test_failed_stop_blocks_claude_preparation(tmp_path: Path, failed: str) -> None:
    package = _fake_claude_code_package(
        tmp_path,
        '#!/bin/sh\necho prepared > "$HOME/prepared-after-failed-stop"\nexit 7\n',
    )
    root = tmp_path / ".claude-smart/claude-code/claude-smart/plugin"
    (root / "scripts").mkdir(parents=True)
    (root / "identity").write_text("old")
    _services(root, failed)
    link = tmp_path / ".reflexio/plugin-root"
    link.parent.mkdir()
    link.symlink_to(root, target_is_directory=True)
    result = _run_fake_claude_code_install(
        tmp_path, package, {"FIXTURE_RUNNING": "both"}
    )
    _assert_rejected(tmp_path, root, result, failed, "both")
