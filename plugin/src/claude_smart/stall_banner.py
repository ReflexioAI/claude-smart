"""Render the 1-line SessionStart banner for a credit/auth stall.

The template branches on the stall reason; output goes through Claude Code's
``additionalContext`` so the model sees it once per stall event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

StallReason = Literal["billing_error", "auth_error"]

_DASHBOARD = "localhost:3001"


def render_banner(*, reason: str | None, reset_estimate: datetime | None) -> str:
    """Format the SessionStart banner for the given stall reason.

    Args:
        reason (str | None): ``"billing_error"`` or ``"auth_error"``. Other values
            (including ``None``) yield an empty string so callers can pass raw DB
            values safely.
        reset_estimate (datetime | None): Best-effort credit reset time;
            included in the billing-error banner when present.

    Returns:
        str: A single-line banner, or ``""`` for unknown reasons.
    """
    match reason:
        case "billing_error":
            if reset_estimate is None:
                return (
                    f"claude-smart: learning paused — Agent SDK credit "
                    f"exhausted. Details: {_DASHBOARD}"
                )
            return (
                f"claude-smart: learning paused — Agent SDK credit "
                f"exhausted (resets ~{_format_reset(reset_estimate)}). "
                f"Details: {_DASHBOARD}"
            )
        case "auth_error":
            return (
                f"claude-smart: learning paused — please run /login. "
                f"Details: {_DASHBOARD}"
            )
        case _:
            return ""


def _format_reset(value: datetime) -> str:
    """Format a reset datetime as e.g. ``Jun 12 9:00`` for the banner.

    Args:
        value (datetime): The reset time to format.

    Returns:
        str: The banner-friendly representation, with the hour shown
            without a leading zero (e.g. ``Jun 12 9:00``).
    """
    return f"{value.strftime('%b %d')} {value.hour}:{value.minute:02d}"
