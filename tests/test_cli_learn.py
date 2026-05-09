"""Tests for ``claude_smart.cli.cmd_learn``."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from claude_smart import cli, state


def _make_args(**overrides: Any) -> argparse.Namespace:
    defaults = {"session": None, "project": "test-project"}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def fake_publish(monkeypatch):
    calls: list[dict[str, Any]] = []
    control: dict[str, Any] = {"return_value": ("ok", 3)}

    def fake(**kwargs):
        calls.append(kwargs)
        return control["return_value"]

    monkeypatch.setattr(cli.publish, "publish_unpublished", fake)
    return calls, control


def test_cmd_learn_publishes_with_force_extraction(
    session_dir, fake_publish, capsys
) -> None:
    state.append("s1", {"role": "User", "content": "hi"})
    state.append("s1", {"role": "Assistant", "content": "answer"})
    calls, _ = fake_publish

    rc = cli.cmd_learn(_make_args())

    assert rc == 0
    assert calls == [
        {
            "session_id": "s1",
            "project_id": "test-project",
            "force_extraction": True,
            "skip_aggregation": True,
        }
    ]
    out = capsys.readouterr().out
    assert "Forced extraction on session `s1`" in out


def test_cmd_learn_does_not_append_synthetic_turn(session_dir, fake_publish) -> None:
    state.append("s1", {"role": "User", "content": "hi"})
    state.append("s1", {"role": "Assistant", "content": "answer"})

    rc = cli.cmd_learn(_make_args())

    assert rc == 0
    records = state.read_all("s1")
    assert len(records) == 2
    assert all("[correction]" not in (r.get("content") or "") for r in records)


def test_cmd_learn_no_active_session_returns_zero(
    session_dir, fake_publish, capsys
) -> None:
    calls, _ = fake_publish

    rc = cli.cmd_learn(_make_args())

    assert rc == 0
    assert calls == []
    assert list(session_dir.glob("*.jsonl")) == []
    assert "No active claude-smart session buffer found" in capsys.readouterr().out


def test_cmd_learn_reflexio_unreachable_returns_one(
    session_dir, fake_publish, capsys
) -> None:
    state.append("s1", {"role": "User", "content": "hi"})
    state.append("s1", {"role": "Assistant", "content": "answer"})
    _, control = fake_publish
    control["return_value"] = ("failed", 2)

    rc = cli.cmd_learn(_make_args())

    assert rc == 1
    assert "Failed to reach reflexio" in capsys.readouterr().out


def test_cmd_learn_nothing_to_publish_returns_zero(
    session_dir, fake_publish, capsys
) -> None:
    state.append("s1", {"role": "User", "content": "hi"})
    _, control = fake_publish
    control["return_value"] = ("nothing", 0)

    rc = cli.cmd_learn(_make_args())

    assert rc == 0
    assert "No unpublished interactions on session `s1`" in capsys.readouterr().out
