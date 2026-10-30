"""Tests for ``claude-smart load``: print an injected item's full text."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from claude_smart import cli, context_format, hook_log, state

REPO_ROOT = Path(__file__).resolve().parents[1]

_ITEM = {
    "user_playbook_id": 17,
    "content": "Run uv sync after pyproject edits.\nThen run an import smoke test.",
    "trigger": "editing pyproject.toml",
    "rationale": "a stale lockfile broke CI",
}

_EXPECTED = (
    "[cs:s1-17] project skill\n"
    "When: editing pyproject.toml\n"
    "Run uv sync after pyproject edits.\nThen run an import smoke test.\n"
    "Why: a stale lockfile broke CI\n"
    "Open: http://localhost:3001/rules/s1-17\n"
)


def _inject(session_id: str) -> str:
    """Record one item the way ``context_inject.emit_context`` does."""
    markdown, registry = context_format.render_inline_with_registry(
        project_id="demo",
        user_playbooks=[_ITEM],
        agent_playbooks=[],
        profiles=[{"profile_id": "pref", "content": "prefers concise answers"}],
        session_id=session_id,
    )
    state.append_injected(session_id, registry)
    return markdown


@pytest.mark.parametrize("item_id", ["s1-17", "cs:s1-17", "[cs:s1-17]"])
def test_load_prints_full_text_of_injected_item(
    session_dir, capsys, item_id: str
) -> None:
    _inject("sess-1")

    assert cli.main(["load", item_id, "--session", "sess-1"]) == 0

    assert capsys.readouterr().out == _EXPECTED


def test_load_prints_preference(session_dir, capsys) -> None:
    _inject("sess-1")

    assert cli.main(["load", "p1-pref", "--session", "sess-1"]) == 0

    assert capsys.readouterr().out == (
        "[cs:p1-pref] preference\n"
        "prefers concise answers\n"
        "Open: http://localhost:3001/rules/p1-pref\n"
    )


def test_load_defaults_to_latest_injected_session(session_dir, capsys) -> None:
    _inject("older")
    os.utime(state.injected_path("older"), (1, 1))
    _inject("newer")
    state.append("older", {"role": "User", "content": "newer session buffer write"})

    assert cli.main(["load", "s1-17"]) == 0

    assert capsys.readouterr().out == _EXPECTED


def test_load_unknown_id_fails_clearly(session_dir, capsys) -> None:
    _inject("sess-1")

    assert cli.main(["load", "s9-zzzz", "--session", "sess-1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown id 's9-zzzz' in session sess-1" in captured.err


def test_load_without_any_injected_session_fails_clearly(session_dir, capsys) -> None:
    assert cli.main(["load", "s1-17"]) == 1

    assert "no claude-smart session has injected learnings yet" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize("session_id", ["../etc", "a/b", ".hidden", ""])
def test_load_rejects_unsafe_session_ids(session_dir, capsys, session_id) -> None:
    _inject("sess-1")

    assert cli.main(["load", "s1-17", "--session", session_id]) == 1

    assert "invalid session id" in capsys.readouterr().err


def test_load_works_while_backend_is_down(session_dir, capsys, monkeypatch) -> None:
    """The loader reads the session registry and never contacts the backend."""

    def backend_down(*_args, **_kwargs):
        raise ConnectionError("backend down")

    _inject("sess-1")
    monkeypatch.setattr(cli, "Adapter", backend_down)
    monkeypatch.setattr("claude_smart.reflexio_adapter.Adapter", backend_down)
    monkeypatch.setenv("REFLEXIO_URL", "http://127.0.0.1:9")

    assert cli.main(["load", "s1-17", "--session", "sess-1"]) == 0

    assert capsys.readouterr().out == _EXPECTED


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell harness")
@pytest.mark.skipif(shutil.which("uv") is None, reason="cli.sh requires uv")
@pytest.mark.parametrize("host", ["claude-code", "codex", "opencode"])
def test_injected_loader_command_runs_from_each_host_shell(
    tmp_path: Path, monkeypatch, host: str
) -> None:
    """Run the exact command the index header shows, as the model would."""
    home = tmp_path / "home"
    (home / ".reflexio").mkdir(parents=True)
    (home / ".reflexio" / "plugin-root").symlink_to(
        REPO_ROOT / "plugin", target_is_directory=True
    )
    state_root = tmp_path / "state"
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(state_root))
    markdown = _inject("sess-1")
    command = f"{context_format.LOADER_COMMAND} --session sess-1"
    assert f"`{command} <id>`" in markdown

    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "CLAUDE_SMART_STATE_DIR": str(state_root),
        "CLAUDE_SMART_HOST": host,
    }
    result = subprocess.run(
        ["bash", "-c", f"{command} s1-17"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == _EXPECTED


def test_load_records_loaded_learning_for_next_turn(session_dir, capsys) -> None:
    _inject("sess-1")
    state.append("sess-1", {"role": "User", "content": "q"})

    assert cli.main(["load", "s1-17", "--session", "sess-1"]) == 0
    assert cli.main(["load", "s1-17", "--session", "sess-1"]) == 0
    state.append("sess-1", {"role": "Assistant", "content": "a"})

    _, turns = state.unpublished_slice(state.read_all("sess-1"))
    assistant = [t for t in turns if t["role"] == "Assistant"]
    assert assistant[0]["retrieved_learnings"] == [
        {"kind": "user_playbook", "learning_id": "17", "loaded": True},
        {"kind": "profile", "learning_id": "pref"},
    ]
    assert capsys.readouterr().out == _EXPECTED * 2
    events = [
        json.loads(line)
        for line in hook_log._LOG_PATH.read_text().splitlines()
        if json.loads(line).get("event") == "load"
    ]
    assert [(e["load_sent"], e["local_only_loads"]) for e in events] == [
        (True, 0),
        (True, 0),
    ]


def test_load_of_published_turn_is_counted_locally_and_not_sent(
    session_dir, capsys
) -> None:
    _inject("sess-1")
    state.append("sess-1", {"role": "Assistant", "content": "a"})
    state.mark_all_published("sess-1")

    assert cli.main(["load", "s1-17", "--session", "sess-1"]) == 0
    state.append("sess-1", {"role": "Assistant", "content": "a2"})

    _, turns = state.unpublished_slice(state.read_all("sess-1"))
    assert [t.get("retrieved_learnings") for t in turns] == [None]
    (event,) = [
        json.loads(line)
        for line in hook_log._LOG_PATH.read_text().splitlines()
        if json.loads(line).get("event") == "load"
    ]
    assert event["load_sent"] is False
    assert event["local_only_loads"] == 1


def test_load_of_id_not_injected_in_session_records_nothing(
    session_dir, capsys
) -> None:
    _inject("other")
    before = state.read_all("sess-1")

    assert cli.main(["load", "s1-17", "--session", "sess-1"]) == 1

    assert state.read_all("sess-1") == before
    assert "unknown id" in capsys.readouterr().err


def test_load_still_prints_when_recording_fails(
    session_dir, capsys, monkeypatch
) -> None:
    _inject("sess-1")

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(state, "append_loaded", broken)

    assert cli.main(["load", "s1-17", "--session", "sess-1"]) == 0
    assert capsys.readouterr().out == _EXPECTED
