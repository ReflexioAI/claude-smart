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


def _git(*args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "protocol.file.allow=always",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _repo(path) -> None:
    _git("init", "-q", str(path))
    _git("-C", str(path), "commit", "-q", "--allow-empty", "-m", "init")


def test_resolve_maps_linked_worktree_to_main_repo(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    main = tmp_path / "repo"
    _repo(main)
    worktree = main / ".claude" / "worktrees" / "feature-x"
    _git("-C", str(main), "worktree", "add", "-q", str(worktree))
    (worktree / "src").mkdir()
    assert ids.resolve_project_id(main) == "repo"
    assert ids.resolve_project_id(worktree) == "repo"
    assert ids.resolve_project_id(worktree / "src") == "repo"


def test_resolve_maps_worktree_outside_main_repo(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    main = tmp_path / "repo"
    _repo(main)
    worktree = tmp_path / "elsewhere" / "repo-feature"
    _git("-C", str(main), "worktree", "add", "-q", str(worktree))
    assert ids.resolve_project_id(worktree) == "repo"


def test_resolve_keeps_separate_git_dir_checkout_on_its_toplevel(
    tmp_path, monkeypatch
) -> None:
    # Its git dir is called ``.git`` but lives elsewhere; it is not a linked
    # worktree, so it must keep its own id (regression guard for review of #195).
    _isolate_git_env(monkeypatch)
    app = tmp_path / "app"
    store = tmp_path / "meta" / "store"
    store.mkdir(parents=True)
    _git("init", "-q", f"--separate-git-dir={store / '.git'}", str(app))
    _git("-C", str(app), "commit", "-q", "--allow-empty", "-m", "init")
    (app / "src").mkdir()
    assert ids.resolve_project_id(app) == "app"
    assert ids.resolve_project_id(app / "src") == "app"


def test_resolve_keeps_submodules_on_their_own_toplevel(tmp_path, monkeypatch) -> None:
    _isolate_git_env(monkeypatch)
    lib = tmp_path / "libsrc"
    _repo(lib)
    main = tmp_path / "repo"
    _repo(main)
    _git("-C", str(main), "submodule", "add", "-q", str(lib), "libsub")
    _git("-C", str(main), "commit", "-q", "-m", "add submodule")
    assert ids.resolve_project_id(main / "libsub") == "libsub"

    worktree = main / ".claude" / "worktrees" / "feat-x"
    _git("-C", str(main), "worktree", "add", "-q", str(worktree))
    _git("-C", str(worktree), "submodule", "update", "-q", "--init")
    assert ids.resolve_project_id(worktree) == "repo"
    assert ids.resolve_project_id(worktree / "libsub") == "libsub"


def test_resolve_keeps_bare_repo_worktree_on_its_toplevel(
    tmp_path, monkeypatch
) -> None:
    # A bare main repo has no checkout to name, so each worktree keeps its id.
    _isolate_git_env(monkeypatch)
    seed = tmp_path / "seed"
    _repo(seed)
    bare = tmp_path / "project.git"
    _git("clone", "-q", "--bare", str(seed), str(bare))
    worktree = tmp_path / "bare-wt"
    _git("-C", str(bare), "worktree", "add", "-q", str(worktree))
    assert ids.resolve_project_id(worktree) == "bare-wt"


def test_resolve_maps_windows_linked_worktree_to_main_repo(monkeypatch) -> None:
    outputs = {
        "rev-parse --git-dir --git-common-dir": (
            "C:/Users/Alice/repo/.git/worktrees/x\nC:/Users/Alice/repo/.git\n"
        ),
        "worktree list --porcelain": (
            "worktree C:/Users/Alice/repo\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree C:/Users/Alice/repo/.claude/worktrees/x\nHEAD abc\n"
        ),
    }

    def _fake(argv, **_kw):
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=outputs[" ".join(argv[1:])]
        )

    monkeypatch.setattr(subprocess, "run", _fake)
    assert ids.resolve_project_id(r"C:\Users\Alice\repo\.claude\worktrees\x") == "repo"


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
