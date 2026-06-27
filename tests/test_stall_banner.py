"""Banner-text rendering for credit-stall notifications."""

from __future__ import annotations

from datetime import datetime, timezone

from claude_smart.stall_banner import render_banner


def test_billing_error_with_reset_estimate():
    text = render_banner(
        reason="billing_error",
        reset_estimate=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
    )
    assert "learning paused" in text
    assert "credit exhausted" in text
    assert "Jun 12" in text
    assert "Jun 12 9:00" in text
    assert "localhost:3001" in text


def test_billing_error_without_reset_estimate_drops_parenthetical():
    text = render_banner(reason="billing_error", reset_estimate=None)
    assert "learning paused" in text
    assert "credit exhausted" in text
    assert "resets" not in text


def test_auth_error_mentions_login():
    text = render_banner(reason="auth_error", reset_estimate=None)
    assert "/login" in text
    assert "credit" not in text


def test_unknown_reason_returns_empty():
    assert render_banner(reason="something_else", reset_estimate=None) == ""
