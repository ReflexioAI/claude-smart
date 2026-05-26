"""Shared ``~/.reflexio/.env`` helpers for claude-smart."""

from __future__ import annotations

import os
from pathlib import Path

REFLEXIO_ENV_PATH = Path.home() / ".reflexio" / ".env"
MANAGED_REFLEXIO_URL = "https://www.reflexio.ai/"
REFLEXIO_URL_ENV = "REFLEXIO_URL"
REFLEXIO_API_KEY_ENV = "REFLEXIO_API_KEY"
CLAUDE_SMART_READ_ONLY_ENV = "CLAUDE_SMART_READ_ONLY"
CLAUDE_SMART_USE_LOCAL_CLI_ENV = "CLAUDE_SMART_USE_LOCAL_CLI"
CLAUDE_SMART_USE_LOCAL_EMBEDDING_ENV = "CLAUDE_SMART_USE_LOCAL_EMBEDDING"

_LOCAL_DEFAULT_ENTRIES = (
    (
        "# Route reflexio generation through the local Claude Code CLI",
        CLAUDE_SMART_USE_LOCAL_CLI_ENV,
        "1",
    ),
    (
        "# Use the in-process ONNX embedder (chromadb) - no API key for semantic search",
        CLAUDE_SMART_USE_LOCAL_EMBEDDING_ENV,
        "1",
    ),
    (None, CLAUDE_SMART_READ_ONLY_ENV, "0"),
)
_LOCAL_MODE_PRUNE_KEYS = {
    REFLEXIO_URL_ENV,
    REFLEXIO_API_KEY_ENV,
    "REFLEXIO_USER_ID",
}


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


def ensure_local_env_defaults(path: Path | None = None) -> list[str]:
    """Create or augment ``~/.reflexio/.env`` for claude-smart local mode.

    Existing active assignments win. This repairs first installs and deleted
    env files without clobbering explicit user overrides such as
    ``CLAUDE_SMART_READ_ONLY=1``.
    """
    path = path or REFLEXIO_ENV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text()
    except OSError:
        existing = ""

    present: set[str] = set()
    kept_lines: list[str] = []
    pruned = False
    for line in existing.splitlines():
        parsed = parse_env_line(line)
        if parsed is not None:
            key, _value = parsed
            if key in _LOCAL_MODE_PRUNE_KEYS:
                pruned = True
                continue
            present.add(key)
        kept_lines.append(line)

    additions: list[str] = []
    added_keys: list[str] = []
    for comment, key, value in _LOCAL_DEFAULT_ENTRIES:
        if key in present:
            continue
        if comment:
            additions.append(comment)
        if key == CLAUDE_SMART_READ_ONLY_ENV:
            additions.append(f'{key}="{_escape_env_value(value)}"')
        else:
            additions.append(f"{key}={_escape_env_value(value)}")
        added_keys.append(key)

    if additions or pruned:
        content = "\n".join(kept_lines)
        if additions:
            prefix = "" if not content or content.endswith("\n") else "\n"
            content = content + prefix + "\n".join(additions)
        content = content + ("\n" if content else "")
        path.write_text(content, encoding="utf-8")
    elif not path.exists():
        path.touch()
    path.chmod(0o600)
    return added_keys


def env_truthy(name: str) -> bool:
    """Return True when an environment flag is explicitly enabled."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


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
