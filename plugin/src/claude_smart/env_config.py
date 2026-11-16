"""Shared ``~/.claude-smart/.env`` helpers for claude-smart.

claude-smart keeps its env under ``~/.claude-smart`` (not ``~/.reflexio``) so an
OSS reflexio backend sharing this machine never leaks its ``.env`` into
claude-smart. The data directory stays shared at ``~/.reflexio/data``. This path
must stay in sync with the ``REFLEXIO_ENV_FILE`` export in
``scripts/backend-service.sh`` and the source path in
``scripts/_lib.sh`` (``claude_smart_source_reflexio_env``).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from filelock import Timeout, UnixFileLock, WindowsFileLock

CLAUDE_SMART_ENV_PATH = Path.home() / ".claude-smart" / ".env"
REFLEXIO_URL_ENV = "REFLEXIO_URL"
CLAUDE_SMART_READ_ONLY_ENV = "CLAUDE_SMART_READ_ONLY"
CLAUDE_SMART_USE_LOCAL_CLI_ENV = "CLAUDE_SMART_USE_LOCAL_CLI"

_LOCAL_DEFAULT_ENTRIES = (
    (
        "# Route local generation through the selected host CLI; "
        "0 uses configured cloud LLM credentials instead.",
        CLAUDE_SMART_USE_LOCAL_CLI_ENV,
        "1",
    ),
    (None, CLAUDE_SMART_READ_ONLY_ENV, "0"),
    (
        "# Local interaction history row cap; 0 disables cap cleanup",
        "REFLEXIO_ROW_LIMIT_INTERACTIONS",
        "500",
    ),
)
_ENV_OVERRIDABLE_LOCAL_DEFAULT_KEYS = {
    CLAUDE_SMART_USE_LOCAL_CLI_ENV,
    "REFLEXIO_ROW_LIMIT_INTERACTIONS",
}
_LOCAL_REFLEXIO_URL_RE = re.compile(
    r"http://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(/?|:.*)"
)


def reflexio_url_is_remote(url: str) -> bool:
    """Mirror ``claude_smart_reflexio_url_is_remote`` in ``scripts/_lib.sh``.

    The runtime starts a local backend unless this is true, so installer
    summaries must use the same rule. The URL is deliberately not stripped:
    the shell helper matches the raw value too.
    """
    return bool(url) and _LOCAL_REFLEXIO_URL_RE.fullmatch(url) is None


def parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse a simple dotenv ``KEY=value`` line.

    This intentionally ignores comments, blank lines, and shell features. The
    Reflexio env writer emits plain quoted assignments, which is all
    claude-smart needs here.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    stripped = re.sub(r"^export\s+", "", stripped)
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
    """Load ``~/.claude-smart/.env`` into ``os.environ`` without overriding values."""
    path = path or CLAUDE_SMART_ENV_PATH
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


class SettingsBusyError(RuntimeError):
    """Another cooperating settings transaction is currently publishing."""


class SettingsConflictError(RuntimeError):
    """Relevant assignments changed after the caller confirmed an edit."""


def update_settings(
    updates: dict[str, str | None],
    expected: dict[str, list[str]],
    path: Path | None = None,
) -> None:
    """Apply confirmed edits under the same lock as dependency repair.

    Expected assignments are the original lines for every edited key and any
    other keys the decision depended on. An empty list means absent, whereas
    an existing empty assignment remains present. Unrelated edits survive.
    Values travel on stdin to the prepared module, never command arguments.
    """
    if not isinstance(updates, dict) or not isinstance(expected, dict):
        raise ValueError("Settings edits require updates and original assignments")
    for key in updates.keys() | expected.keys():
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None
        ):
            raise ValueError("Invalid settings key")
    if updates.keys() - expected.keys():
        raise ValueError("Settings edits require the original edited assignments")
    for key, lines in expected.items():
        if not isinstance(lines, list) or any(
            not isinstance(line, str)
            or not _single_line_value(line)
            or (entry := parse_env_line(line)) is None
            or entry[0] != key
            for line in lines
        ):
            raise ValueError("Invalid original settings assignments")
    for value in updates.values():
        # Use the shared lossless unquoted subset. Escaped quoted writes have
        # different meanings to the shell/Node readers and python-dotenv.
        # Existing unrelated values are retained verbatim, not re-encoded.
        if value is not None and (
            not isinstance(value, str)
            or not _single_line_value(value)
            or value != value.strip()
            or value.startswith(("'", '"'))
            or "${" in value
            or re.search(r"\s#", value)
        ):
            raise ValueError("Settings value cannot be represented consistently")

    def edit(existing: str) -> tuple[str | None, None]:
        # Match the shell/Node physical-line boundary, retaining every original
        # delimiter. str.splitlines() also splits valid unrelated value bytes
        # such as U+2028 and must not turn those into new assignments.
        spans = re.findall(r"[^\n]*\n|[^\n]+$", existing)
        lines = [
            span[:-2] if span.endswith("\r\n") else span.removesuffix("\n")
            for span in spans
        ]
        originals: dict[str, list[str]] = {key: [] for key in expected}
        for line in lines:
            if (entry := parse_env_line(line)) is not None and entry[0] in originals:
                originals[entry[0]].append(line)
        if originals != expected:
            raise SettingsConflictError(
                "Settings changed after confirmation; reload and retry"
            )
        seen: set[str] = set()
        output: list[str] = []
        for span, line in zip(spans, lines, strict=True):
            entry = parse_env_line(line)
            if entry is None or entry[0] not in updates:
                output.append(span)
                continue
            key = entry[0]
            if key not in seen and updates[key] is not None:
                output.append(f"{key}={updates[key]}" + span[len(line) :])
            seen.add(key)
        if not updates:
            return None, None
        content = "".join(output)
        newline = next(
            (
                "\r\n" if span.endswith("\r\n") else "\n"
                for span in spans
                if span.endswith("\n")
            ),
            "\n",
        )
        for key, value in updates.items():
            if key not in seen and value is not None:
                if content and not content.endswith("\n"):
                    content += newline
                content += f"{key}={value}" + newline
        return content, None

    _edit_env_file(path or CLAUDE_SMART_ENV_PATH, edit)


def repair_bootstrap_env(path: Path | None = None) -> list[str]:
    """Supply missing local defaults without selecting or changing a connection.

    Setup owns the URL, credentials, sharing and learning choices. Dependency
    repair preserves those decisions, including explicit empties and duplicates.
    Remote URL alone means attached mode, even when its credentials need repair.
    """
    return _edit_env_file(path or CLAUDE_SMART_ENV_PATH, _bootstrap_defaults)


def _bootstrap_defaults(existing: str) -> tuple[str | None, list[str]]:
    current = {}
    for line in existing.splitlines():
        if (entry := parse_env_line(line)) is not None:
            current[entry[0]] = entry[1]
    url = current.get(REFLEXIO_URL_ENV, os.environ.get(REFLEXIO_URL_ENV, ""))
    if reflexio_url_is_remote(url):
        return None, []
    additions = []
    added = []
    for comment, key, default in _LOCAL_DEFAULT_ENTRIES:
        if key in current:
            continue
        value = _resolve_local_env_default(key, default)
        if comment:
            additions.append(comment)
        if key == CLAUDE_SMART_READ_ONLY_ENV:
            additions.append(f'{key}="{value}"')
        else:
            additions.append(f"{key}={value}")
        added.append(key)
    if not additions:
        return existing, added
    separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    return existing + separator + "\n".join(additions) + "\n", added


def _regular_env_file(path: Path) -> bool:
    """Reject special files and links instead of replacing their destinations."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(
            "Environment settings and lock must be regular, unlinked files"
        )
    return True


def _edit_env_file[Result](
    path: Path, edit: Callable[[str], tuple[str | None, Result]]
) -> Result:
    """Own one helper's read/transform/publication, not an entire installation.

    Only participating writers serialize. There is no retry, soft-lock fallback,
    or stale-marker deletion. os.replace is the commit point; directory/power-loss
    durability and compensation after a lost acknowledgement are not promised.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    _regular_env_file(lock_path)
    native_lock = WindowsFileLock if os.name == "nt" else UnixFileLock
    # Require the declared native-backend contract, including failure cleanup.
    # Keep one marker identity and refuse a filesystem without native locking.
    lock = native_lock(
        lock_path,
        timeout=0,
        mode=0o600,
        fallback_to_soft=False,
        preserve_lock_file=True,
    )
    try:
        with lock:
            exists = _regular_env_file(path)
            if exists:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                with os.fdopen(os.open(path, flags), "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("Environment settings must be a regular file")
                    try:
                        original = source.read().decode("utf-8")
                    except UnicodeDecodeError:
                        raise ValueError(
                            "Environment settings must be valid UTF-8"
                        ) from None
            else:
                original = ""
            content, result = edit(original)
            if content is None:
                return result
            # No-op operations still repair the original private mode, as before.
            if exists and content == original:
                path.chmod(0o600)
                return result
            # Python >=3.12.4 gives a 0700 directory a private Windows DACL,
            # too. Its child inherits that access before any secret is written;
            # a same-volume rename retains it even in a broadly readable parent.
            with tempfile.TemporaryDirectory(
                prefix=".env-", dir=path.parent
            ) as staging:
                temporary = Path(staging) / "settings"
                flags = (
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                )
                fd = os.open(temporary, flags, 0o600)
                with os.fdopen(fd, "wb") as target:
                    target.write(content.encode("utf-8"))
                    target.flush()
                    os.fsync(target.fileno())
                if exists and os.name == "nt":
                    # Match the old in-place writer's read-only-attribute repair.
                    path.chmod(0o600)
                temporary.replace(path)
            return result
    except Timeout:
        raise SettingsBusyError(
            "Environment settings are busy; retry after the other save finishes"
        ) from None


def _resolve_local_env_default(key: str, fallback: str) -> str:
    if key in _ENV_OVERRIDABLE_LOCAL_DEFAULT_KEYS:
        explicit = os.environ.get(key, "").strip()
        if explicit:
            if not _single_line_value(explicit):
                raise ValueError("Invalid local environment default")
            return explicit
    return fallback


def env_truthy(name: str) -> bool:
    """Return True when an environment flag is explicitly enabled."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _single_line_value(value: str) -> bool:
    # Readers use str.splitlines(), whose boundaries include Unicode separators
    # as well as CR/LF. Values must never become additional assignments there.
    return "\0" not in value and value.splitlines() in ([], [value])


def main(argv: list[str] | None = None) -> int:
    """Internal prepared-runtime entrypoint; no bootstrap or provider imports."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in (["bootstrap-repair"], ["publish"]):
        print(
            "Usage: python -m claude_smart.env_config {bootstrap-repair|publish}",
            file=sys.stderr,
        )
        return 2
    try:
        if arguments == ["publish"]:
            request = json.load(sys.stdin)
            if not isinstance(request, dict) or set(request) != {"updates", "expected"}:
                raise ValueError("Invalid settings edit request")
            update_settings(request["updates"], request["expected"])
            added = []
        else:
            added = repair_bootstrap_env()
    except SettingsConflictError as error:
        print(str(error), file=sys.stderr)
        return 73
    except SettingsBusyError as error:
        print(str(error), file=sys.stderr)
        return 75
    except (OSError, ValueError):
        print(
            "Cannot update settings; check file access and single-line UTF-8 values.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, "added": added}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
