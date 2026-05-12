"""Tests for project-id resolution."""

from __future__ import annotations

import subprocess

from claude_smart import ids


_GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)


def _isolate_git_env(monkeypatch) -> None:
    # Pre-commit/rebase contexts inherit GIT_DIR + friends that would override
    # cwd-based discovery in subprocess git calls and break tmp_path isolation.
    for name in _GIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_resolve_uses_git_toplevel_basename(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subdir = tmp_path / "packages" / "core"
    subdir.mkdir(parents=True)
    assert ids.resolve_project_id(subdir) == tmp_path.name


def test_resolve_falls_back_to_cwd_basename_outside_git(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    # tmp_path from pytest is not inside a git repo.
    assert ids.resolve_project_id(tmp_path) == tmp_path.name


def test_resolve_handles_missing_git_binary(tmp_path, monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise FileNotFoundError("git missing")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert ids.resolve_project_id(tmp_path) == tmp_path.name
