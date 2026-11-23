"""Shared ``~/.claude-smart/.env`` helpers for claude-smart.

claude-smart keeps its env under ``~/.claude-smart`` (not ``~/.reflexio``) so an
OSS reflexio backend sharing this machine never leaks its ``.env`` into
claude-smart. The data directory stays shared at ``~/.reflexio/data``. This path
must stay in sync with the ``REFLEXIO_ENV_FILE`` export in
``scripts/backend-service.sh`` and the source path in
``scripts/_lib.sh`` (``claude_smart_source_reflexio_env``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from filelock import Timeout, UnixFileLock, WindowsFileLock

CLAUDE_SMART_ENV_PATH = Path.home() / ".claude-smart" / ".env"
LEGACY_REFLEXIO_ENV_PATH = Path.home() / ".reflexio" / ".env"
INSTALL_COMPLETION_COMMENT = "# claude-smart: legacy-env checked"
REFLEXIO_URL_ENV = "REFLEXIO_URL"
CLAUDE_SMART_READ_ONLY_ENV = "CLAUDE_SMART_READ_ONLY"
NATIVE_MODEL_ENV = "REFLEXIO_NATIVE_CLI_MODEL"
RETIRED_LEARNING_ENVS = frozenset(
    {
        "CLAUDE_SMART_USE_LOCAL_CLI",
        "CLAUDE_SMART_LEARNING_PROVIDER",
        "CLAUDE_SMART_LEARNING_MODEL",
    }
)

_LOCAL_DEFAULT_ENTRIES = (
    (None, CLAUDE_SMART_READ_ONLY_ENV, "0"),
    (
        "# Local interaction history row cap; 0 disables cap cleanup",
        "REFLEXIO_ROW_LIMIT_INTERACTIONS",
        "500",
    ),
)
_ENV_OVERRIDABLE_LOCAL_DEFAULT_KEYS = {"REFLEXIO_ROW_LIMIT_INTERACTIONS"}
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
    # A UTF-8 BOM is file metadata, matching Node's leading trim behavior.
    stripped = line.strip().removeprefix("\ufeff").lstrip()
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
        # Text-mode universal newlines would turn a literal CR into a binding boundary.
        text = path.read_bytes().decode("utf-8")
    except OSError:
        return
    for line in text.split("\n"):
        parsed = parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key in RETIRED_LEARNING_ENVS or key == NATIVE_MODEL_ENV:
            # Only the backend child reads saved execution intent. Hook/client
            # processes must not turn it into a stale inherited override.
            continue
        # Shell hooks and status select the first nonempty saved session directory.
        if key == "CLAUDE_SMART_STATE_DIR" and not os.environ.get(key):
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


class SettingsBusyError(RuntimeError):
    """Another cooperating settings transaction is currently publishing."""


class SettingsPreflightError(RuntimeError):
    """The staged settings failed a prepared-runtime prerequisite."""

    def __init__(self, code: int) -> None:
        super().__init__("Prepared runtime preflight failed")
        self.code = code


class SettingsConflictError(RuntimeError):
    """Relevant assignments changed after the caller confirmed an edit."""


def _literal_env_value(value: str) -> str:
    """Encode without the escape/interpolation differences between readers."""
    if not isinstance(value, str) or not _single_line_value(value) or "${" in value:
        raise ValueError("Settings value cannot be represented consistently")
    if (
        value == value.strip()
        and not value.startswith(("'", '"', "\ufeff"))
        and not value.endswith("\ufeff")
        and re.search(r"\s#", value) is None
    ):
        return value
    # A trailing backslash escapes the added quote and can consume later rows.
    if value.endswith("\\"):
        raise ValueError("Settings value cannot be represented consistently")
    # dotenv decodes backslash pairs inside single quotes. The shell/Node
    # readers only remove the outer quotes, so never introduce escaping.
    if "'" not in value and "\\\\" not in value:
        return "'" + value + "'"
    if '"' not in value and re.search(r"""\\[\\'"abfnrtv]""", value) is None:
        return '"' + value + '"'
    raise ValueError("Settings value cannot be represented consistently")


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
    edit = _settings_editor(updates, expected)
    validate_native = bool(
        updates.keys() & (RETIRED_LEARNING_ENVS | {NATIVE_MODEL_ENV})
    )
    if REFLEXIO_URL_ENV in updates:
        original_urls = expected[REFLEXIO_URL_ENV]
        previous_url = (
            (parse_env_line(original_urls[-1]) or ("", ""))[1]
            if original_urls
            else os.environ.get(REFLEXIO_URL_ENV, "")
        )
        requested_url = updates[REFLEXIO_URL_ENV]
        if requested_url is None:
            requested_url = os.environ.get(REFLEXIO_URL_ENV, "")
        validate_native |= reflexio_url_is_remote(
            previous_url
        ) and not reflexio_url_is_remote(requested_url)
    _edit_env_file(
        path or CLAUDE_SMART_ENV_PATH,
        edit,
        validate=_verify_staged_native_selection if validate_native else None,
    )


def _settings_editor(
    updates: dict[str, str | None], expected: dict[str, list[str]]
) -> Callable[[str], tuple[str | None, None]]:
    """Build the same pure edit for dashboard and confirmed installation."""
    if not isinstance(updates, dict) or not isinstance(expected, dict):
        raise TypeError("Settings edits require updates and original assignments")
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
    encoded = {
        key: _literal_env_value(value) if value is not None else None
        for key, value in updates.items()
    }

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
                output.append(f"{key}={encoded[key]}" + span[len(line) :])
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
        for key, value in encoded.items():
            if key not in seen and value is not None:
                if content and not content.endswith("\n"):
                    content += newline
                content += f"{key}={value}" + newline
        return content, None

    return edit


def publish_install(
    updates: dict[str, str | None],
    snapshot: dict[str, object],
    legacy: dict[str, object] | None,
    *,
    prepared: bool = False,
    check_windows_embedding: bool = False,
    opencode_loader: object = None,
) -> None:
    """Publish a confirmed guide snapshot, imports and completion atomically.

    Node selects provenance while building its one preview. This owner verifies
    that source and destination have not changed; it does not reinterpret the
    guide's effective-value or connection-selection policies. A prepared
    runtime supplies its missing local defaults in the same transaction.
    """
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "exists",
        "sha256",
        "legacy_marker",
    }:
        raise ValueError("Invalid installation snapshot")
    existed, digest, marker = (
        snapshot["exists"],
        snapshot["sha256"],
        snapshot["legacy_marker"],
    )
    if (
        not isinstance(existed, bool)
        or not isinstance(marker, bool)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("Invalid installation snapshot")
    if not isinstance(updates, dict):
        raise TypeError("Invalid installation updates")
    # Validate canonical edits before entering the transaction, as publish does.
    _settings_editor(updates, {key: [] for key in updates})
    legacy_digest = None
    selected_rows = {}
    if legacy is not None:
        if not isinstance(legacy, dict) or set(legacy) != {"sha256", "rows"}:
            raise ValueError("Invalid legacy selection")
        source_digest, source_rows = legacy["sha256"], legacy["rows"]
        if (
            not isinstance(source_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
            or not isinstance(source_rows, dict)
        ):
            raise ValueError("Invalid legacy selection")
        legacy_digest, selected_rows = source_digest, dict(source_rows)

    path = CLAUDE_SMART_ENV_PATH
    marker_path = path.parent / "legacy-reflexio-env-checked"

    def edit(existing: str) -> tuple[str, None]:
        if _regular_env_file(marker_path) != marker:
            raise SettingsConflictError("Migration status changed; reload and retry")
        checked = (
            existing.removeprefix("\ufeff").partition("\n")[0].removesuffix("\r")
            == INSTALL_COMPLETION_COMMENT
        )
        content = existing
        newline = "\r\n" if "\r\n" in existing else "\n"
        if legacy_digest is not None:
            if marker or checked:
                raise SettingsConflictError(
                    "Legacy migration completed; reload and retry"
                )
            for row in _legacy_import_rows(
                legacy_digest, selected_rows, existing, updates
            ):
                if content and not content.endswith("\n"):
                    content += newline
                content += row + newline
        expected = {key: [] for key in updates}
        for line in content.split("\n"):
            line = line.removesuffix("\r")
            if (entry := parse_env_line(line)) is not None and entry[0] in expected:
                expected[entry[0]].append(line)
        edited, _ = _settings_editor(updates, expected)(content)
        if edited is not None:
            content = edited
        if not checked:
            bom = "\ufeff" if existing.startswith("\ufeff") else ""
            content = (
                bom
                + INSTALL_COMPLETION_COMMENT
                + newline
                + content.removeprefix("\ufeff")
            )
        if prepared:
            with_defaults, _ = _bootstrap_defaults(content)
            if with_defaults is not None:
                content = with_defaults
        return content, None

    loader = (
        _opencode_loader_snapshot(opencode_loader)
        if opencode_loader is not None
        else None
    )

    with ExitStack() as open_files:

        def validate(staged: Path) -> None:
            if updates.keys() & (RETIRED_LEARNING_ENVS | {NATIVE_MODEL_ENV}):
                _verify_staged_native_selection(staged)
            if check_windows_embedding:
                _verify_staged_windows_embedding(staged)
            if loader is not None:
                _verify_opencode_loader(*loader, open_files)

        _edit_env_file(
            path,
            edit,
            expected_snapshot=(existed, digest),
            validate=validate
            if check_windows_embedding
            or loader is not None
            or updates.keys() & (RETIRED_LEARNING_ENVS | {NATIVE_MODEL_ENV})
            else None,
        )


def _legacy_import_rows(
    legacy_digest: str,
    selected_rows: dict[object, object],
    existing: str,
    updates: dict[str, str | None],
) -> list[str]:
    """Validate the captured fixed source and retain only selected raw RHS bytes."""
    source_exists, source = _read_env_file(LEGACY_REFLEXIO_ENV_PATH)
    if not source_exists or hashlib.sha256(source).hexdigest() != legacy_digest:
        raise SettingsConflictError("Legacy settings changed; reload and retry")
    rows = source.decode("utf-8").split("\n")
    present = {
        entry[0]
        for line in existing.split("\n")
        if (entry := parse_env_line(line)) is not None
    }
    imported = []
    for key, index in selected_rows.items():
        if (
            not isinstance(key, str)
            or re.fullmatch(r"CLAUDE_SMART_[A-Z0-9_]+", key) is None
            or key == "CLAUDE_SMART_USE_LOCAL_EMBEDDING"
            or key in updates
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(rows)
        ):
            raise ValueError("Invalid legacy row selection")
        if key in present:
            raise SettingsConflictError("Legacy destination exists; reload and retry")
        row = rows[index].removesuffix("\r")
        entry = parse_env_line(row)
        if entry is None or entry[0] != key or not _composable_legacy_row(row):
            raise ValueError("Legacy row cannot be transferred safely")
        imported.append(key + "=" + row.partition("=")[2])
    return imported


def _composable_legacy_row(row: str) -> bool:
    """Refuse cross-row quotes and interpolation dependent on the old file."""
    # Horizontal tabs are valid export/assignment whitespace; other control
    # bytes cannot be safely carried into another consumer's settings.
    if (
        not _single_line_value(row)
        or re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", row)
        or "${" in row
    ):
        return False
    rhs = row.partition("=")[2].strip()
    if rhs.startswith(("'", '"')):
        quote = rhs[0]
        return (
            len(rhs) >= 2
            and rhs[-1] == quote
            and quote not in rhs[1:-1]
            and not rhs[1:-1].endswith("\\")
        )
    return True


def repair_bootstrap_env(path: Path | None = None) -> list[str]:
    """Supply missing local defaults without selecting or changing a connection.

    Setup owns the URL, credentials, sharing and learning choices. Dependency
    repair preserves those decisions, including explicit empties and duplicates.
    Remote URL alone means attached mode, even when its credentials need repair.
    """
    return _edit_env_file(path or CLAUDE_SMART_ENV_PATH, _bootstrap_defaults)


def _bootstrap_defaults(existing: str) -> tuple[str | None, list[str]]:
    current = {}
    for line in existing.split("\n"):
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
    separator = "" if not existing or existing.endswith("\n") else "\n"
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


def _read_env_file(path: Path) -> tuple[bool, bytes]:
    """Read the same regular, unlinked file contract for target and provenance."""
    if not _regular_env_file(path):
        return False, b""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Environment settings must be a regular, unlinked file")
        return True, source.read()


def _edit_env_file[Result](
    path: Path,
    edit: Callable[[str], tuple[str | None, Result]],
    *,
    expected_snapshot: tuple[bool, str] | None = None,
    validate: Callable[[Path], None] | None = None,
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
            exists, source = _read_env_file(path)
            if expected_snapshot is not None and expected_snapshot != (
                exists,
                hashlib.sha256(source).hexdigest(),
            ):
                raise SettingsConflictError(
                    "Settings changed after confirmation; reload and retry"
                )
            try:
                original = source.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("Environment settings must be valid UTF-8") from None
            content, result = edit(original)
            if content is None:
                return result
            # No-op operations still repair the original private mode, as before.
            unchanged = exists and content == original
            if unchanged and validate is None:
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
                if validate is not None:
                    validate(temporary)
                    # An external editor does not participate in this lock.
                    # Recheck the original after the potentially slow probe.
                    if _read_env_file(path) != (exists, source):
                        raise SettingsConflictError(
                            "Settings changed after confirmation; reload and retry"
                        )
                if unchanged or (exists and os.name == "nt"):
                    # Match the old in-place writer's read-only-attribute repair.
                    path.chmod(0o600)
                if not unchanged:
                    # Retiring saved selectors is a one-way, confirmed edit.
                    # Keep exact original bytes under the same private writer
                    # after validation, before committing their replacement.
                    before_keys = {
                        entry[0]
                        for line in original.split("\n")
                        if (entry := parse_env_line(line)) is not None
                    }
                    after_keys = {
                        entry[0]
                        for line in content.split("\n")
                        if (entry := parse_env_line(line)) is not None
                    }
                    if before_keys & RETIRED_LEARNING_ENVS and not (
                        after_keys & RETIRED_LEARNING_ENVS
                    ):
                        history = path.parent / "native-selection-history"
                        snapshot = history / (
                            hashlib.sha256(source).hexdigest() + ".env"
                        )

                        def retain(previous: str) -> tuple[str, None]:
                            if os.path.lexists(snapshot) and previous != original:
                                raise SettingsConflictError(
                                    "Saved selection history changed"
                                )
                            return original, None

                        _edit_env_file(snapshot, retain)
                    temporary.replace(path)
            return result
    except Timeout:
        raise SettingsBusyError(
            "Environment settings are busy; retry after the other save finishes"
        ) from None


def _opencode_loader_snapshot(
    snapshot: object,
) -> tuple[Path, bytes | None, str | None]:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "path",
        "contents",
        "identity",
    }:
        raise ValueError("Invalid OpenCode loader snapshot")
    path, contents, identity = (
        snapshot["path"],
        snapshot["contents"],
        snapshot["identity"],
    )
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or (
            identity is not None
            if contents is None
            else not isinstance(contents, str)
            or not isinstance(identity, str)
            or re.fullmatch(r"[0-9]+:[0-9]+", identity) is None
        )
    ):
        raise ValueError("Invalid OpenCode loader snapshot")
    return (
        Path(path),
        contents.encode("utf-8") if contents is not None else None,
        identity,
    )


def _verify_opencode_loader(
    path: Path, expected: bytes | None, identity: str | None, open_files: ExitStack
) -> None:
    """Check the scoped loader and retain its file handle through settings save."""
    # The transient install-settings publisher does not bundle host adapters.
    from claude_smart.opencode_scope import open_scoped_file

    try:
        source, opened_identity = open_scoped_file(path, open_files)
        if (
            identity == opened_identity
            and expected is not None
            and source.read(len(expected) + 1) == expected
        ):
            return
    except FileNotFoundError:
        if expected is None:
            return
    except (OSError, ValueError):
        pass
    raise SettingsPreflightError(80)


def _verify_staged_native_selection(staged: Path) -> None:
    """Use the prepared SDK before committing a changed native selection."""
    values = {}
    nonempty_saved: set[str] = set()
    native_references: set[str] = set()
    for line in staged.read_bytes().decode("utf-8").split("\n"):
        if (entry := parse_env_line(line)) is not None:
            values[entry[0]] = entry[1]
            if entry[1].strip():
                nonempty_saved.add(entry[0])
            if entry[0] == NATIVE_MODEL_ENV:
                native_references.add(entry[1])
    if len(native_references) > 1:
        raise SettingsPreflightError(81)
    desired = values.get(NATIVE_MODEL_ENV, "")
    inherited = os.environ.get(NATIVE_MODEL_ENV)
    if inherited is not None and inherited != desired:
        raise SettingsConflictError(
            "An exported native target would hide this saved change; clear it and retry"
        )
    if reflexio_url_is_remote(
        values.get(REFLEXIO_URL_ENV, os.environ.get(REFLEXIO_URL_ENV, ""))
    ):
        return  # Inactive local preferences never activate a managed backend.
    if any(
        name in values or os.environ.get(name, "") for name in RETIRED_LEARNING_ENVS
    ) or (
        "CLAUDE_SMART_CLI_MODEL" in nonempty_saved
        or os.environ.get("CLAUDE_SMART_CLI_MODEL", "").strip()
    ):
        raise SettingsPreflightError(81)
    if not desired.strip():
        # Disabling native generation needs no provider import or executable.
        # SDK-free connection/cloud repairs must remain possible.
        return
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "claude_smart.native_selection",
            ],
            cwd=staged.parent,
            env={**os.environ, "REFLEXIO_ENV_FILE": str(staged)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SettingsPreflightError(81) from None
    if result.returncode:
        raise SettingsPreflightError(81)


def _verify_staged_windows_embedding(staged: Path) -> None:
    root = Path.cwd()
    # The SDK gives a working-directory dotenv file precedence. A bundled
    # runtime must see the saved account file when it later starts from root.
    if os.path.lexists(root / ".env"):
        raise SettingsPreflightError(79)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(root / "scripts/backend-inference-owner.py"),
                "--validate-windows-embedding",
            ],
            cwd=staged.parent,
            env={**os.environ, "REFLEXIO_ENV_FILE": str(staged)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SettingsPreflightError(78) from None
    except OSError:
        raise SettingsPreflightError(76) from None
    if result.returncode:
        raise SettingsPreflightError(77 if result.returncode == 77 else 76)


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
    # Keep newly published values in the portable single-line subset. Existing
    # opaque spans are retained verbatim by the edit path.
    return "\0" not in value and value.splitlines() in ([], [value])


def main(argv: list[str] | None = None) -> int:
    """Internal prepared-runtime entrypoint; no bootstrap or provider imports."""
    arguments = sys.argv[1:] if argv is None else argv
    commands = {
        "bootstrap-repair",
        "publish",
        "publish-install",
        "publish-prepared-install",
    }
    if (
        not arguments
        or arguments[0] not in commands
        or (
            len(arguments) != 1
            and arguments != ["publish-prepared-install", "--check-windows-embedding"]
        )
    ):
        print(
            "Usage: python -m claude_smart.env_config "
            "{bootstrap-repair|publish|publish-install|publish-prepared-install}",
            file=sys.stderr,
        )
        return 2
    command = arguments[0]
    try:
        if command == "bootstrap-repair":
            added = repair_bootstrap_env()
        else:
            # Node sends JSON bytes; a Windows console code page must not
            # corrupt values or original-assignment comparisons under -I.
            request = json.load(sys.stdin.buffer)
            fields = (
                {"updates", "expected"}
                if command == "publish"
                else {"updates", "snapshot", "legacy"}
            )
            if not isinstance(request, dict):
                raise ValueError("Invalid settings edit request")
            if command == "publish-prepared-install" and "opencode_loader" in request:
                fields.add("opencode_loader")
            if set(request) != fields:
                raise ValueError("Invalid settings edit request")
            if command == "publish":
                update_settings(request["updates"], request["expected"])
            else:
                publish_install(
                    request["updates"],
                    request["snapshot"],
                    request["legacy"],
                    prepared=command == "publish-prepared-install",
                    check_windows_embedding=len(arguments) == 2,
                    opencode_loader=request.get("opencode_loader"),
                )
            added = []
    except SettingsConflictError as error:
        print(str(error), file=sys.stderr)
        return 73
    except SettingsBusyError as error:
        print(str(error), file=sys.stderr)
        return 75
    except SettingsPreflightError as error:
        if error.code == 81:
            print(
                "Native learning target failed prepared SDK validation; settings were not saved.",
                file=sys.stderr,
            )
        return error.code
    except (OSError, ValueError, TypeError):
        print(
            "Cannot update settings; check file access and single-line UTF-8 values.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, "added": added}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
