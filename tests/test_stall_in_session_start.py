"""SessionStart should prepend the stall banner exactly once per stall event."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_smart.events import session_start


@pytest.fixture
def stalled_state():
    return SimpleNamespace(
        stalled=True,
        reason="billing_error",
        stalled_at=datetime.now(timezone.utc),
        reset_estimate=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        notified_in_cc=False,
        error_message="x",
    )


@pytest.fixture
def clean_state():
    return SimpleNamespace(
        stalled=False,
        reason=None,
        stalled_at=None,
        reset_estimate=None,
        notified_in_cc=False,
        error_message=None,
    )


def _capture_output(payload):
    """Run session_start.handle with stdout captured. Returns the additionalContext."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        session_start.handle(payload)
    out = buf.getvalue().strip()
    if not out:
        return ""
    return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_stalled_unnotified_prepends_banner(stalled_state, monkeypatch, session_dir):
    monkeypatch.setattr(
        "claude_smart.events.session_start._adapter",
        lambda: _AdapterStub(stalled_state),
    )
    text = _capture_output({"session_id": "s1", "cwd": "/tmp"})
    assert "learning paused" in text
    assert text.startswith("claude-smart: learning paused")


def test_stalled_unnotified_calls_mark_notified(stalled_state, monkeypatch, session_dir):
    stub = _AdapterStub(stalled_state)
    monkeypatch.setattr("claude_smart.events.session_start._adapter", lambda: stub)
    _capture_output({"session_id": "s1", "cwd": "/tmp"})
    assert stub.notified_calls == 1


def test_stalled_already_notified_does_not_prepend(
    stalled_state, monkeypatch, session_dir
):
    stalled_state.notified_in_cc = True
    monkeypatch.setattr(
        "claude_smart.events.session_start._adapter",
        lambda: _AdapterStub(stalled_state),
    )
    text = _capture_output({"session_id": "s1", "cwd": "/tmp"})
    assert "learning paused" not in text


def test_clean_state_no_banner(clean_state, monkeypatch, session_dir):
    monkeypatch.setattr(
        "claude_smart.events.session_start._adapter",
        lambda: _AdapterStub(clean_state),
    )
    text = _capture_output({"session_id": "s1", "cwd": "/tmp"})
    assert "learning paused" not in text


def test_adapter_unreachable_does_not_crash(monkeypatch, session_dir):
    monkeypatch.setattr(
        "claude_smart.events.session_start._adapter",
        lambda: _AdapterStub(None),
    )
    text = _capture_output({"session_id": "s1", "cwd": "/tmp"})
    assert "learning paused" not in text  # silent skip


class _AdapterStub:
    def __init__(self, state):
        self._state = state
        self.notified_calls = 0

    def fetch_stall_state(self):
        return self._state

    def mark_stall_notified(self):
        self.notified_calls += 1

    # Mirror the rest of Adapter's API as needed — return [] / no-op.
    def fetch_both(self, **kw):
        return [], []

    def apply_batch_defaults(self, **kw):
        return True
