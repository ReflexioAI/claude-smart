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


def test_resolve_handles_windows_cwd_when_git_fails(monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise FileNotFoundError("git missing")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert ids.resolve_project_id(r"C:\Users\Alice\repo") == "repo"


def test_resolve_handles_windows_git_toplevel(monkeypatch) -> None:
    def _git(*_a, **_kw):
        return subprocess.CompletedProcess(
            args=["git", "rev-parse", "--show-toplevel"],
            returncode=0,
            stdout="C:\\Users\\Alice\\repo\n",
        )

    monkeypatch.setattr(subprocess, "run", _git)
    assert ids.resolve_project_id(r"C:\Users\Alice\repo\packages\core") == "repo"


def test_resolve_project_id_ignores_user_id_override(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    monkeypatch.setenv("REFLEXIO_API_KEY", "rflx-test-key")
    monkeypatch.setenv("REFLEXIO_USER_ID", "override-user")

    # resolve_project_id always reflects the working directory; the override
    # only applies to resolve_user_id.
    assert ids.resolve_project_id(tmp_path) == tmp_path.name


def test_resolve_user_id_returns_env_override(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    monkeypatch.setenv("REFLEXIO_USER_ID", "override-user")
    assert ids.resolve_user_id(tmp_path) == "override-user"


def test_resolve_user_id_falls_back_to_project_id(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    monkeypatch.delenv("REFLEXIO_USER_ID", raising=False)
    assert ids.resolve_user_id(tmp_path) == tmp_path.name


def test_resolve_user_id_ignores_blank_override(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    monkeypatch.setenv("REFLEXIO_USER_ID", "   ")
    assert ids.resolve_user_id(tmp_path) == tmp_path.name
