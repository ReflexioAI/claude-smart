"""Resolve user-facing links to the Claude Smart dashboard."""

import os


def dashboard_base_url() -> str:
    """Use an explicit public origin or the configured local listener port."""
    explicit = os.environ.get("CLAUDE_SMART_DASHBOARD_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    port = os.environ.get("DASHBOARD_PORT", "").strip()
    if port.isascii() and port.isdecimal() and len(port) <= 5:
        number = int(port)
        if 1 <= number <= 65535:
            return f"http://localhost:{number}"
    return "http://localhost:3001"
