"""Shared fixtures for claude-smart tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    """Redirect the state dir to a per-test tmp path."""
    monkeypatch.setenv("CLAUDE_SMART_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def clear_optimizer_opt_in(monkeypatch):
    """Keep env-gated optimizer setup from leaking into unrelated tests."""
    monkeypatch.delenv("CLAUDE_SMART_ENABLE_OPTIMIZER", raising=False)


@pytest.fixture(autouse=True)
def reset_runtime_host(monkeypatch):
    """Keep host-specific tests from leaking runtime state."""
    from claude_smart import runtime

    monkeypatch.delenv("CLAUDE_SMART_HOST", raising=False)
    runtime.set_host(runtime.HOST_CLAUDE_CODE)
    yield
    runtime.set_host(runtime.HOST_CLAUDE_CODE)
