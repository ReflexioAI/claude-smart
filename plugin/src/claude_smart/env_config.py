"""Shared ``~/.reflexio/.env`` helpers for claude-smart."""

from __future__ import annotations

import os
from pathlib import Path

REFLEXIO_ENV_PATH = Path.home() / ".reflexio" / ".env"
MANAGED_REFLEXIO_URL = "https://www.reflexio.ai/"
REFLEXIO_URL_ENV = "REFLEXIO_URL"
REFLEXIO_API_KEY_ENV = "REFLEXIO_API_KEY"


def parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse a simple dotenv ``KEY=value`` line.

    This intentionally ignores comments, blank lines, and shell features. The
    Reflexio env writer emits plain quoted assignments, which is all
    claude-smart needs here.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, sep, raw_value = stripped.partition("=")
    if not sep:
        return None
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None
    value = raw_value.strip()
    if len(value) >= 2 and (
        (value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")
    ):
        value = value[1:-1]
    return key, value


def load_reflexio_env(path: Path | None = None) -> None:
    """Load ``~/.reflexio/.env`` into ``os.environ`` without overriding values."""
    path = path or REFLEXIO_ENV_PATH
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        parsed = parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def set_env_vars(path: Path, values: dict[str, str]) -> list[str]:
    """Upsert dotenv keys while preserving comments and unrelated entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text()
    except OSError:
        existing = ""

    lines = existing.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        parsed = parse_env_line(line)
        if parsed is None:
            out.append(line)
            continue
        key, _old = parsed
        if key in values:
            out.append(f'{key}="{_escape_env_value(values[key])}"')
            seen.add(key)
        else:
            out.append(line)

    added: list[str] = []
    for key, value in values.items():
        if key in seen:
            continue
        out.append(f'{key}="{_escape_env_value(value)}"')
        added.append(key)

    content = "\n".join(out)
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")
    path.chmod(0o600)
    return added


def mask_secret(value: str) -> str:
    """Return a display-safe API key preview."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    prefix = value[:5] if "-" in value[:8] else value[:4]
    return f"{prefix}****{value[-4:]}"


def _escape_env_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
