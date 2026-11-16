"""User-facing CLI for claude-smart.

Exposes the following subcommands:

Installation, update and removal are owned by ``npx claude-smart``. This
module operates an already prepared runtime; it never registers integrations.

- ``show``: print current project-specific skills and project preferences
  (as markdown).
- ``learn``: publish unpublished interactions and force reflexio
  extraction now over the active session buffer.
- ``load``: print the full text of a ``[cs:ID]`` item a hook injected as an
  index line. The model runs it through ``context_format.LOADER_COMMAND``.
- ``restart``: stop and restart the reflexio backend + dashboard services
  (rebuilding the dashboard bundle) so local Reflexio checkout edits or
  ``plugin/dashboard/`` edits take effect without restarting Claude Code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from claude_smart import (
    context_format,
    cs_cite,
    env_config,
    ids,
    publish,
    state,
)
from claude_smart.reflexio_adapter import Adapter

_CLAUDE_SMART_ENV_PATH = env_config.CLAUDE_SMART_ENV_PATH
_REFLEXIO_UNREACHABLE_MSG = (
    "Failed to reach reflexio. Check ~/.claude-smart/backend.log "
    "or restart Claude Code.\n"
)

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _THIS_DIR.parents[1]  # plugin/src/claude_smart/ -> plugin/
_SCRIPTS_DIR = _PLUGIN_ROOT / "scripts"
_DASHBOARD_DIR = _PLUGIN_ROOT / "dashboard"
_BACKEND_SCRIPT = _SCRIPTS_DIR / "backend-service.sh"
_DASHBOARD_SCRIPT = _SCRIPTS_DIR / "dashboard-service.sh"
_REFLEXIO_DIR = Path.home() / ".reflexio"
_SERVICE_STATUS_PROBE_FAILED = "probe_failed"
_DEFAULT_STORAGE_ROOT = _REFLEXIO_DIR / "data"
# Org id claude-smart's backend runs under (set via REFLEXIO_DEFAULT_ORG_ID in
# backend-service.sh). reflexio names the config file config_<org>.json, so this
# must stay in sync with that export; using our own org keeps us off the
# enterprise self-host backend's config_self-host-org.json.
_REFLEXIO_ORG_ID = "claude-smart"
_REFLEXIO_CONFIG_PATH = _REFLEXIO_DIR / "configs" / f"config_{_REFLEXIO_ORG_ID}.json"
_LOCAL_STORAGE_ENV = "LOCAL_STORAGE_PATH"


def _windows_path_text(path: str) -> str:
    """Normalize slashes/case for Windows path checks."""
    return path.replace("/", "\\").lower()


_WINDOWS_SYSTEM_BASH_SUFFIXES = (
    "\\windows\\system32\\bash.exe",
    "\\windows\\sysnative\\bash.exe",
    "\\windows\\syswow64\\bash.exe",
)


def _is_windows_system_bash(path: str) -> bool:
    normalized = _windows_path_text(path)
    return normalized.endswith(_WINDOWS_SYSTEM_BASH_SUFFIXES)


def _resolve_candidate(candidate: str) -> str | None:
    # Keep unknown ~user text available for PATH lookup; Path.expanduser raises.
    expanded = os.path.expandvars(os.path.expanduser(candidate))  # noqa: PTH111
    if os.path.isfile(expanded):  # noqa: PTH113 — preserve native string command resolution.
        return expanded
    return shutil.which(expanded)


def _iter_path_candidates(names: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir:
            continue
        directory = os.path.expandvars(os.path.expanduser(raw_dir))  # noqa: PTH111 — keep unknown ~user literal.
        for name in names:
            candidate = os.path.join(directory, name)  # noqa: PTH118 — preserve executable path spelling.
            if os.path.isfile(candidate):  # noqa: PTH113 — native string command lookup.
                found.append(candidate)
    return found


def _first_non_system_bash(candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        resolved = _resolve_candidate(candidate)
        if resolved and not _is_windows_system_bash(resolved):
            return resolved
    return None


def _resolve_bash() -> str | None:
    """Resolve a bash executable suitable for running bundled shell scripts.

    Returns:
        Absolute path to a usable bash, or None when none is available.
        On Windows, the WSL stub under ``\\Windows\\System32`` (and the
        ``Sysnative`` / ``SysWOW64`` redirectors) is rejected so that bundled
        ``*.sh`` scripts run under Git-Bash instead.
    """
    if os.name != "nt":
        return shutil.which("bash")

    sources: list[Iterable[str]] = []
    bash_env = os.environ.get("BASH", "").strip()
    if bash_env:
        sources.append((bash_env,))
    sources.append(
        (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        )
    )
    sources.append(_iter_path_candidates(("bash.exe", "bash")))
    sources.append(("bash.exe", "bash"))

    for source in sources:
        resolved = _first_non_system_bash(source)
        if resolved:
            return resolved
    return None


def _resolve_npm() -> str | None:
    """Resolve npm, accounting for Windows .cmd/.exe shims."""
    if os.name == "nt":
        for candidate in ("npm.cmd", "npm.exe", "npm"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None
    return shutil.which("npm")


def _latest_session_id() -> str | None:
    """Most-recently-modified session JSONL in the state dir. None if none exist."""
    root = state.state_dir()
    if not root.is_dir():
        return None
    files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    return files[0].stem


# Plugins-directory loader, mirroring the Node installer byte-for-byte: each
# side must recognize (and refresh or remove) what the other wrote.


def cmd_show(args: argparse.Namespace) -> int:
    """Print this project's skills + preferences, plus globally-shared skills.

    User playbooks (this project's skills) and preferences are scoped via
    ``project_id`` (resolved from cwd via ``ids.resolve_project_id``).
    Agent playbooks are global by construction — they're aggregated across
    every project, so lessons learned anywhere surface here.

    Args:
        args (argparse.Namespace): Parsed CLI args. Honors ``args.project``
            (override project id for the project-scoped fetches).

    Returns:
        int: 0 on success.
    """
    project_id = args.project or ids.resolve_user_id()
    adapter = Adapter()
    user_playbooks, agent_playbooks, profiles = adapter.fetch_all(
        project_id=project_id,
        user_playbook_top_k=3,
        agent_playbook_top_k=3,
        profile_top_k=3,
    )
    md = context_format.render(
        project_id=project_id,
        user_playbooks=user_playbooks,
        agent_playbooks=agent_playbooks,
        profiles=profiles,
    )
    if not md and adapter.read_errors:
        sys.stdout.write(
            "Could not read skills or preferences from Reflexio for "
            f"project `{project_id}` at `{adapter.url}`.\n"
            f"First error: {adapter.read_errors[0]}\n"
        )
        return 1
    body = md or f"_No skills or preferences yet for project `{project_id}`._\n"
    sys.stdout.write(body + _reflexio_show_footer())
    return 0


def _reflexio_show_footer() -> str:
    """Return the markdown attribution footer appended to ``show`` output.

    Reinforces that claude-smart is powered by the open-source reflexio engine
    and nudges users to star the repo.

    Returns:
        str: A leading-separator markdown footer ending in a newline.
    """
    return (
        f"\n---\nclaude-smart is powered by [reflexio]({cs_cite.REFLEXIO_REPO_URL})"
        f" — if it helps you, [star it on GitHub]({cs_cite.REFLEXIO_REPO_URL}).\n"
    )


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def cmd_load(args: argparse.Namespace) -> int:
    """Print the full text of one or more injected ``[cs:ID]`` items.

    Reads the session's citation registry, which already holds each shown
    item's content, trigger and rationale, so loading needs no backend call
    and works offline, in managed mode, and after the item is edited
    server-side (it prints what was retrieved for this session).

    Items are printed in the order requested; a repeated id is printed once.
    An unknown id is reported on stderr and does not stop the others.

    Args:
        args (argparse.Namespace): ``args.ids`` are the item ids, each with
            or without the ``cs:`` prefix or brackets; ``args.session``
            overrides the session (defaults to the most recently injected
            one).

    Returns:
        int: 0 when every requested item was printed, 1 when any id is
            unknown or the session is missing or invalid.
    """
    item_ids = list(
        dict.fromkeys(raw.strip().strip("[]").removeprefix("cs:") for raw in args.ids)
    )
    session_id = (
        args.session if args.session is not None else _latest_injected_session_id()
    )
    if session_id is not None and not _SESSION_ID_RE.match(session_id):
        sys.stderr.write(f"error: invalid session id {session_id!r}\n")
        return 1
    if not session_id:
        sys.stderr.write(
            "error: no claude-smart session has injected learnings yet; "
            "pass --session <id> from the injected load command.\n"
        )
        return 1
    registry = state.read_injected(session_id)
    status = 0
    for item_id in item_ids:
        entry = registry.get(item_id)
        if entry is None:
            sys.stderr.write(
                f"error: unknown id {item_id!r} in session {session_id}. Use an id "
                "from a [cs:…] line injected in this session.\n"
            )
            status = 1
            continue
        sys.stdout.write(context_format.render_loaded_item(entry))
    return status


def _latest_injected_session_id() -> str | None:
    """Session whose citation registry was written most recently."""
    root = state.state_dir()
    if not root.is_dir():
        return None
    suffix = ".injected.jsonl"
    files = sorted(
        root.glob(f"*{suffix}"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return files[0].name[: -len(suffix)] if files else None


def cmd_learn(args: argparse.Namespace) -> int:
    """Force reflexio extraction over the active session's interactions.

    Publishes unpublished interactions with ``force_extraction=True`` and
    ``override_learning_stall=True`` as an explicit retry request rather than
    waiting for the next batch interval. The HTTP round trip waits for acceptance;
    extraction runs asynchronously via ``wait_for_response=False``. Recovery
    confirms storage only and cannot verify or rerun the extraction options.
    When ``args.note`` is provided, the note is appended to the session
    buffer as a neutral User turn before publishing — letting the user
    capture an explicit insight, preference, or workflow note alongside
    whatever has already been buffered.

    Args:
        args (argparse.Namespace): Parsed CLI args. Honors ``args.session``
            (defaults to most-recent), ``args.project`` (defaults to
            ``ids.resolve_project_id()``), and ``args.note`` (free-form
            text appended as a User turn before publish).

    Returns:
        int: 0 on success or no-op (no active session, or nothing to
            publish), 1 if reflexio is unreachable or recovery could not confirm
            the requested extraction options.
    """
    if env_config.env_truthy(env_config.CLAUDE_SMART_READ_ONLY_ENV):
        sys.stdout.write(
            "claude-smart is in read-only mode (CLAUDE_SMART_READ_ONLY=1); "
            "skipping publish.\n"
        )
        return 0
    session_id = args.session or _latest_session_id()
    if not session_id:
        sys.stdout.write("No active claude-smart session buffer found.\n")
        return 0
    project_id = args.project or ids.resolve_user_id()

    note = (args.note or "").strip()
    if note:
        state.append(
            session_id,
            {
                "ts": int(time.time()),
                "role": "User",
                "content": note,
                "user_id": project_id,
            },
        )

    status, count = publish.publish_unpublished(
        session_id=session_id,
        project_id=project_id,
        force_extraction=True,
        override_learning_stall=True,
        skip_aggregation=False,
    )
    if status == "ok":
        suffix = " (including your note)" if note else ""
        sys.stdout.write(
            f"Requested forced extraction on session `{session_id}` over {count} "
            f"interactions{suffix}.\n"
        )
        return 0
    if status == "recovered":
        sys.stdout.write(
            f"Recovered stored publication for session `{session_id}` over {count} "
            f"interactions.\n"
            "Recovery confirms storage only; forced extraction, stall override, "
            "and aggregation options were not verified or rerun.\n"
            "Run learn again without --note to publish any remaining buffered "
            "interactions.\n"
            "If none remain, a new interaction or note can request extraction.\n"
        )
        return 1
    if status == "nothing":
        sys.stdout.write(f"No unpublished interactions on session `{session_id}`.\n")
        return 0
    if status == "parked":
        sys.stdout.write(
            f"Reflexio repeatedly rejected a buffered batch on session `{session_id}`; "
            "it was skipped so later interactions can publish.\n"
            "Its `publish_dead_letter` record in the session buffer keeps the "
            "request ID, range and reason. The local interactions remain there; "
            "retrying will not resend that range. "
            "See `publish-parked` in ~/.claude-smart/hook.log.\n"
        )
        return 1
    sys.stdout.write(_REFLEXIO_UNREACHABLE_MSG)
    return 1


def _run_service(script: Path, subcmd: str) -> int:
    """Invoke a service script (``backend-service.sh`` / ``dashboard-service.sh``).

    Args:
        script (Path): Absolute path to the service shell script.
        subcmd (str): Subcommand to pass (``start``, ``stop``, ``status``).

    Returns:
        int: The script's exit code, or 1 if the script is missing.
    """
    if not script.exists():
        sys.stderr.write(f"error: {script} not found\n")
        return 1
    bash = _resolve_bash()
    if not bash:
        sys.stderr.write(
            "error: bash is required to run claude-smart service scripts\n"
        )
        return 1
    try:
        subprocess.run(  # noqa: S603 — repository service script and fixed operation.
            [bash, str(script), subcmd], check=True
        )
        return 0
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1


def _service_status(script: Path, wait_ready_s: float = 3.0) -> str:
    """Return the one-line status string for a service script.

    Service scripts spawn their targets detached and return immediately, so
    a status probe fired right after ``start`` can race the child's cold
    boot (e.g. Next.js takes ~150ms to bind). Poll briefly until the script
    reports something other than ``not running`` or the deadline expires.

    Args:
        script (Path): Path to the service script.
        wait_ready_s (float): Max seconds to wait for a ready status before
            returning the last observed value.

    Returns:
        str: One-line status string (e.g. ``"running on http://..."`` or
        ``"not running"``).
    """
    if not script.exists():
        return "script missing"
    bash = _resolve_bash()
    if not bash:
        return _SERVICE_STATUS_PROBE_FAILED
    deadline = time.monotonic() + wait_ready_s
    while True:
        result = subprocess.run(  # noqa: S603 — repository service script and fixed status argv.
            [bash, str(script), "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        status = result.stdout.strip() or "unknown"
        if status != "not running" or time.monotonic() >= deadline:
            return status
        time.sleep(0.2)


class _ClearAllError(RuntimeError):
    """Raised when a destructive clear-all target is unsafe or unsupported."""


@dataclass(frozen=True)
class _ClearAllTarget:
    """One filesystem target removed by ``clear-all``."""

    path: Path
    kind: str
    label: str


def _is_running_status(status: str) -> bool:
    return status.startswith("running on ")


def _is_confirmed_stopped_status(status: str) -> bool:
    return status == "not running"


def _read_dotenv_value(env_path: Path, key: str) -> str | None:
    """Read one simple KEY=VALUE binding from a dotenv file."""
    if not env_path.is_file():
        return None
    try:
        # Match the shared settings grammar and physical LF boundaries: a
        # Unicode line separator can be part of the configured storage path.
        lines = env_path.read_bytes().decode("utf-8").split("\n")
    except OSError:
        return None
    for raw_line in lines:
        entry = env_config.parse_env_line(raw_line)
        if entry is not None and entry[0] == key:
            return entry[1].strip()
    return None


def _resolve_absolute_path(raw_path: str | Path, *, source: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise _ClearAllError(f"{source} must be an absolute path: {raw_path}")
    return path


def _effective_storage_root() -> Path:
    raw = os.environ.get(_LOCAL_STORAGE_ENV, "").strip()
    if not raw:
        raw = _read_dotenv_value(_CLAUDE_SMART_ENV_PATH, _LOCAL_STORAGE_ENV) or ""
    return _resolve_absolute_path(
        raw or _DEFAULT_STORAGE_ROOT, source=_LOCAL_STORAGE_ENV
    )


def _load_reflexio_config() -> dict[str, object] | None:
    if not _REFLEXIO_CONFIG_PATH.is_file():
        return None
    try:
        loaded = json.loads(_REFLEXIO_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise _ClearAllError(
            f"could not read reflexio config {_REFLEXIO_CONFIG_PATH}: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise _ClearAllError(
            f"reflexio config {_REFLEXIO_CONFIG_PATH} is not a JSON object"
        )
    return loaded


def _storage_config_kind(storage_config: dict[str, object]) -> str:
    if storage_config.get("managed_by") == "platform":
        return "remote"
    explicit_type = str(
        storage_config.get("type") or storage_config.get("storage_type") or ""
    ).lower()
    if explicit_type in {"supabase", "postgres"}:
        return "remote"
    if explicit_type == "disk":
        return "disk"
    if explicit_type == "sqlite":
        return "sqlite"
    if (
        "url" in storage_config
        and "key" in storage_config
        and "db_url" in storage_config
    ):
        return "remote"
    if "db_url" in storage_config:
        return "remote"
    if "dir_path" in storage_config:
        return "disk"
    if "db_path" in storage_config or not storage_config:
        return "sqlite"
    raise _ClearAllError(
        "unsupported reflexio storage_config shape; refusing to delete local data"
    )


def _dangerous_clear_all_paths() -> set[Path]:
    home = Path.home().resolve(strict=False)
    reflexio_dir = _resolve_absolute_path(_REFLEXIO_DIR, source="reflexio dir")
    return {
        Path("/").resolve(strict=False),
        home,
        reflexio_dir.resolve(strict=False),
        _resolve_absolute_path(
            reflexio_dir / "configs", source="reflexio configs"
        ).resolve(strict=False),
        _PLUGIN_ROOT.resolve(strict=False),
        _PLUGIN_ROOT.parent.resolve(strict=False),
    }


def _validate_deletion_target(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise _ClearAllError(f"refusing to delete symlink target: {path}")
    resolved = path.resolve(strict=False)
    if resolved in _dangerous_clear_all_paths():
        raise _ClearAllError(f"refusing to delete dangerous path: {resolved}")


def _disk_org_targets(base_dir: Path) -> list[_ClearAllTarget]:
    if base_dir.exists() and base_dir.is_symlink():
        raise _ClearAllError(
            f"refusing to inspect symlink disk storage dir: {base_dir}"
        )
    if not base_dir.exists():
        return []
    if not base_dir.is_dir():
        raise _ClearAllError(
            f"configured disk storage path is not a directory: {base_dir}"
        )
    return [
        _ClearAllTarget(child, "dir", "disk org data")
        for child in sorted(base_dir.glob("disk_*"))
    ]


def _resolve_clear_all_targets() -> list[_ClearAllTarget]:
    targets = [
        _ClearAllTarget(_effective_storage_root(), "dir", "managed local storage root")
    ]

    config = _load_reflexio_config()
    storage_config = config.get("storage_config") if config else None
    if storage_config is not None:
        if not isinstance(storage_config, dict):
            raise _ClearAllError("reflexio storage_config is not a JSON object")
        kind = _storage_config_kind(storage_config)
        if kind == "remote":
            raise _ClearAllError(
                "clear-all only resets local Reflexio storage; "
                "remote Supabase/Postgres storage is not supported"
            )
        if kind == "sqlite":
            raw_db_path = storage_config.get("db_path")
            if isinstance(raw_db_path, str) and raw_db_path.strip():
                db_path = _resolve_absolute_path(
                    raw_db_path.strip(), source="configured SQLite db_path"
                )
                targets.extend(
                    _ClearAllTarget(
                        Path(f"{db_path}{suffix}"),
                        "file",
                        "configured SQLite data",
                    )
                    for suffix in ("", "-wal", "-shm", "-journal")
                )
        elif kind == "disk":
            raw_dir_path = storage_config.get("dir_path")
            if not isinstance(raw_dir_path, str) or not raw_dir_path.strip():
                raise _ClearAllError("configured disk storage is missing dir_path")
            disk_base = _resolve_absolute_path(
                raw_dir_path.strip(), source="configured disk dir_path"
            )
            targets.extend(_disk_org_targets(disk_base))

    deduped: list[_ClearAllTarget] = []
    seen: set[Path] = set()
    for target in targets:
        _validate_deletion_target(target.path)
        resolved = target.path.resolve(strict=False)
        if resolved in seen:
            continue
        deduped.append(_ClearAllTarget(resolved, target.kind, target.label))
        seen.add(resolved)
    return deduped


def _remove_clear_all_target(target: _ClearAllTarget) -> bool:
    """Remove a target. Returns True when something was actually removed."""
    path = target.path
    if not path.exists():
        return False
    if target.kind == "dir":
        if not path.is_dir():
            raise _ClearAllError(
                f"expected directory target but found non-directory: {path}"
            )
        shutil.rmtree(path)
        return True
    if target.kind == "file":
        if not path.is_file():
            raise _ClearAllError(f"expected file target but found non-file: {path}")
        path.unlink()
        return True
    raise _ClearAllError(f"unknown clear-all target kind: {target.kind}")


def cmd_restart(args: argparse.Namespace) -> int:
    """Restart the reflexio backend and claude-smart dashboard services.

    Stops both long-lived services, optionally rebuilds the dashboard's
    Next.js bundle so source edits under ``plugin/dashboard/`` take effect,
    then starts them again. Useful during local development when iterating
    on the local Reflexio checkout or the dashboard.

    Args:
        args (argparse.Namespace): Parsed CLI args. Honors ``args.skip_backend``,
            ``args.skip_dashboard``, and ``args.no_rebuild``.

    Returns:
        int: 0 on success, non-zero if the dashboard rebuild fails or
            either service's ``start`` subcommand exits non-zero.
    """
    do_backend = not args.skip_backend
    do_dashboard = not args.skip_dashboard

    if not (do_backend or do_dashboard):
        sys.stdout.write("Nothing to restart (both services skipped).\n")
        return 0

    if do_backend:
        # Preparing the replacement can reinstall the vendored Reflexio if its
        # version drifted, so this both validates and repairs. It must succeed
        # before we stop anything: a backend we cannot replace is better left
        # running.
        sys.stdout.write("Preparing reflexio backend replacement…\n")
        preflight_rc = _run_service(_BACKEND_SCRIPT, "preflight")
        if preflight_rc != 0:
            sys.stderr.write(
                "error: reflexio backend replacement failed preflight; "
                "existing services were not changed.\n"
                "Repair the bundled runtime with: npx claude-smart update\n"
            )
            return preflight_rc

        sys.stdout.write("Stopping reflexio backend…\n")
        _run_service(_BACKEND_SCRIPT, "stop")
    if do_dashboard:
        sys.stdout.write("Stopping dashboard…\n")
        _run_service(_DASHBOARD_SCRIPT, "stop")

    if do_dashboard and not args.no_rebuild:
        if not _DASHBOARD_DIR.is_dir():
            sys.stderr.write(
                f"warning: dashboard dir {_DASHBOARD_DIR} missing; skipping rebuild\n"
            )
        elif not (npm := _resolve_npm()):
            sys.stderr.write("warning: npm not on PATH; serving previous build\n")
        else:
            next_bin = _DASHBOARD_DIR / "node_modules" / ".bin" / "next"
            if not next_bin.exists():
                install_cmd = (
                    [npm, "ci", "--omit=dev", "--no-audit", "--no-fund"]
                    if (_DASHBOARD_DIR / "package-lock.json").exists()
                    else [npm, "install", "--omit=dev", "--no-audit", "--no-fund"]
                )
                install_label = " ".join(install_cmd)
                sys.stdout.write(
                    "Installing dashboard dependencies "
                    f"({install_label}, may take a minute)…\n"
                )
                try:
                    subprocess.run(  # noqa: S603 — resolved package manager and fixed install argv.
                        install_cmd,
                        cwd=_DASHBOARD_DIR,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    sys.stderr.write(
                        f"error: {install_label} failed (exit {exc.returncode}); "
                        "not starting dashboard.\n"
                    )
                    if do_backend:
                        sys.stdout.write("Starting reflexio backend…\n")
                        _run_service(_BACKEND_SCRIPT, "start")
                        sys.stdout.write(
                            f"reflexio backend: {_service_status(_BACKEND_SCRIPT)}\n"
                        )
                    return exc.returncode or 1
            sys.stdout.write(
                "Rebuilding dashboard (npm run build, may take a minute)…\n"
            )
            try:
                subprocess.run(  # noqa: S603 — resolved package manager and fixed build argv.
                    [npm, "run", "build"], cwd=_DASHBOARD_DIR, check=True
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                returncode = getattr(exc, "returncode", 1) or 1
                sys.stderr.write(
                    f"error: dashboard build failed (exit {returncode}); "
                    "not starting dashboard.\n"
                )
                if do_backend:
                    sys.stdout.write("Starting reflexio backend…\n")
                    _run_service(_BACKEND_SCRIPT, "start")
                    sys.stdout.write(
                        f"reflexio backend: {_service_status(_BACKEND_SCRIPT)}\n"
                    )
                return returncode

    start_rc = 0
    if do_backend:
        sys.stdout.write("Starting reflexio backend…\n")
        rc = _run_service(_BACKEND_SCRIPT, "start")
        if rc != 0:
            sys.stderr.write(f"error: reflexio backend failed to start (exit {rc})\n")
            start_rc = rc
    if do_dashboard:
        sys.stdout.write("Starting dashboard…\n")
        rc = _run_service(_DASHBOARD_SCRIPT, "start")
        if rc != 0:
            sys.stderr.write(f"error: dashboard failed to start (exit {rc})\n")
            start_rc = start_rc or rc

    sys.stdout.write("\n")
    if do_backend:
        sys.stdout.write(f"reflexio backend: {_service_status(_BACKEND_SCRIPT)}\n")
    if do_dashboard:
        sys.stdout.write(f"dashboard: {_service_status(_DASHBOARD_SCRIPT)}\n")
    return start_rc


def cmd_clear_all(args: argparse.Namespace) -> int:
    """Delete all local reflexio data and claude-smart session buffers.

    Stops the managed backend first when it is running, wipes local Reflexio
    data targets, removes local session JSONL buffers under ``state_dir()``,
    then restarts the backend if it was running before. Requires ``--yes``.

    Args:
        args (argparse.Namespace): Parsed CLI args. Honors ``args.yes``
            (skip the confirmation prompt).

    Returns:
        int: 0 on success, 1 if reset is unsafe, unsupported, or fails.
    """
    try:
        targets = _resolve_clear_all_targets()
    except _ClearAllError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if not args.yes:
        target_lines = "\n".join(
            f"  - {target.path} ({target.label})" for target in targets
        )
        sys.stdout.write(
            "This will permanently delete ALL local reflexio data at:\n"
            f"{target_lines}\n"
            f"and local session buffers under {state.state_dir()}.\n"
            "If the reflexio backend is running, it will be stopped first "
            "and restarted afterward.\n"
            "Re-run with --yes to confirm.\n"
        )
        return 1

    initial_status = _service_status(_BACKEND_SCRIPT, wait_ready_s=0.0)
    was_running = _is_running_status(initial_status)
    if not was_running and not _is_confirmed_stopped_status(initial_status):
        sys.stderr.write(
            "error: could not confirm reflexio backend is stopped "
            f"({initial_status}); aborting without deleting data\n"
        )
        return 1
    if was_running:
        sys.stdout.write("Stopping reflexio backend…\n")
        stop_rc = _run_service(_BACKEND_SCRIPT, "stop")
        if stop_rc != 0:
            sys.stderr.write(
                f"error: reflexio backend failed to stop (exit {stop_rc})\n"
            )
            return stop_rc or 1
        stopped_status = _service_status(_BACKEND_SCRIPT, wait_ready_s=0.0)
        if _is_running_status(stopped_status):
            sys.stderr.write(
                "error: reflexio backend is still running after stop; "
                "aborting without deleting data\n"
            )
            return 1
        if not _is_confirmed_stopped_status(stopped_status):
            sys.stderr.write(
                "error: could not confirm reflexio backend stopped "
                f"({stopped_status}); aborting without deleting data\n"
            )
            return 1

    removed_targets = 0
    try:
        for target in targets:
            if _remove_clear_all_target(target):
                removed_targets += 1
    except (OSError, _ClearAllError) as exc:
        sys.stderr.write(f"error: could not remove reflexio data: {exc}\n")
        return 1

    removed_buffers = 0
    root = state.state_dir()
    if root.is_dir():
        for buf in root.glob("*.jsonl"):
            try:
                buf.unlink()
                removed_buffers += 1
            except OSError as exc:
                sys.stderr.write(f"warning: could not remove {buf}: {exc}\n")

    start_rc = 0
    backend_status = "not running"
    if was_running:
        sys.stdout.write("Starting reflexio backend…\n")
        start_rc = _run_service(_BACKEND_SCRIPT, "start")
        backend_status = _service_status(_BACKEND_SCRIPT)
        if start_rc != 0:
            sys.stderr.write(
                f"error: reflexio backend failed to start (exit {start_rc})\n"
            )
        elif not _is_running_status(backend_status):
            sys.stderr.write(
                f"error: reflexio backend did not report running: {backend_status}\n"
            )
            start_rc = 1

    target_summary = (
        f"removed {removed_targets} data target(s)"
        if removed_targets
        else "nothing to wipe"
    )
    sys.stdout.write(
        f"Cleared reflexio: {target_summary}. "
        f"Removed {removed_buffers} local session buffer(s).\n"
    )
    if was_running:
        sys.stdout.write(f"reflexio backend: {backend_status}\n")
    else:
        sys.stdout.write("reflexio backend was not running; left stopped.\n")
    return start_rc or 0


def _resolve_reflexio_url() -> str:
    """Return the URL endpoint for the local claude-smart reflexio backend.

    Mirrors ``claude_smart.reflexio_adapter`` so the CLI's one-shot
    ReflexioClient calls hit the same server as the long-lived adapter
    used by hook handlers.

    Returns:
        str: The ``REFLEXIO_URL`` env var if set, otherwise the default
            local backend endpoint.
    """
    env_config.load_reflexio_env()
    return os.environ.get("REFLEXIO_URL", "http://localhost:8071/")


def _resolve_reflexio_api_key() -> str:
    env_config.load_reflexio_env()
    return os.environ.get("REFLEXIO_API_KEY", "")


def _format_deleted_counts(deleted_counts: object) -> str:
    """Render ``deleted_counts`` from a ``clear_user_data`` response as a summary.

    The backend returns one count per affected table. We surface the
    total in the headline string (``"removed N row(s)"``) and append a
    per-table breakdown when at least one bucket is populated, so the
    operator can tell *what* got cleared at a glance.

    Args:
        deleted_counts (object): The ``deleted_counts`` field from the
            backend response. Expected to be a mapping of
            ``str -> int``; anything else is rendered as ``"unknown"``.

    Returns:
        str: A short human-readable summary suitable for echoing to
            stdout (e.g. ``"3 row(s) (interactions=3)"``).
    """
    if not isinstance(deleted_counts, dict):
        return "unknown row(s)"
    pairs: list[tuple[str, int]] = []
    for key, value in deleted_counts.items():
        if isinstance(key, str) and isinstance(value, int):
            pairs.append((key, value))
    total = sum(value for _, value in pairs)
    if not pairs:
        return f"{total} row(s)"
    breakdown = ", ".join(f"{name}={count}" for name, count in sorted(pairs))
    return f"{total} row(s) ({breakdown})"


def cmd_clear_user(args: argparse.Namespace) -> int:
    """Delete one user_id's playbooks, profiles, and interactions via the backend.

    Per-user-scoped counterpart of ``clear-all``. Where ``clear-all`` does
    a filesystem-level wipe of the entire local storage root, ``clear-user``
    routes through the running reflexio backend's ``clear_user_data``
    endpoint so it can scope the deletion to a single ``user_id`` —
    leaving other users' data and globally-shared ``agent_playbooks``
    untouched. This is the primitive SWE-bench-style benchmark harnesses
    need when running many parallel evaluations under distinct synthetic
    user ids against one shared backend.

    Args:
        args (argparse.Namespace): Parsed CLI args. Requires
            ``args.user_id`` (positional) and ``args.yes`` (the
            destructive-action confirmation flag, matching ``clear-all``).

    Returns:
        int: 0 on success, 1 if confirmation is missing or the backend
            call fails.
    """
    user_id = args.user_id
    if not args.yes:
        sys.stdout.write(
            f"This will permanently delete all reflexio rows for user '{user_id}' "
            "(playbooks, profiles, interactions). Other users' data and "
            "agent_playbooks are not affected.\n"
            "Re-run with --yes to confirm.\n"
        )
        return 1

    try:
        from reflexio import ReflexioClient  # type: ignore[import-not-found]
    except ImportError as exc:
        sys.stderr.write(f"error: reflexio is not installed: {exc}\n")
        return 1

    try:
        client = ReflexioClient(
            url_endpoint=_resolve_reflexio_url(),
            api_key=_resolve_reflexio_api_key(),
        )
        # The companion reflexio PR adds ``clear_user_data``; keep the
        # CLI buildable until both sides ship together.
        response = client.clear_user_data(user_id)  # pyright: ignore[reportAttributeAccessIssue]
    except Exception as exc:  # noqa: BLE001 — surface backend failure verbatim.
        sys.stderr.write(f"error: clear-user failed: {exc}\n")
        return 1

    deleted_counts = (
        response.get("deleted_counts") if isinstance(response, dict) else None
    )
    summary = _format_deleted_counts(deleted_counts)
    sys.stdout.write(f"Cleared user '{user_id}': removed {summary}.\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-smart")
    sub = parser.add_subparsers(dest="command", required=True)

    sh = sub.add_parser(
        "show",
        help="Show current project-specific skills and project preferences",
    )
    sh.add_argument("--project", help="Override project id")
    sh.set_defaults(func=cmd_show)

    ln = sub.add_parser(
        "learn",
        help="Force reflexio extraction over the active session now",
    )
    ln.add_argument("--session", help="Session id (defaults to latest)")
    ln.add_argument("--project", help="Override project id")
    ln.add_argument(
        "--note",
        default=None,
        help=(
            "Free-form note to publish as a User turn before extraction "
            "(e.g. an insight, preference, or workflow rule)"
        ),
    )
    ln.set_defaults(func=cmd_learn)

    ld = sub.add_parser(
        "load",
        help="Print the full text of injected [cs:ID] learnings",
    )
    ld.add_argument("ids", nargs="+", help="One or more injected item ids")
    ld.add_argument(
        "--session", help="Session id (defaults to the latest injected session)"
    )
    ld.set_defaults(func=cmd_load)

    ca = sub.add_parser(
        "clear-all",
        help="Delete all local reflexio data and restart the backend",
    )
    ca.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the destructive clear without prompting",
    )
    ca.set_defaults(func=cmd_clear_all)

    cu = sub.add_parser(
        "clear-user",
        help=(
            "Delete one user_id's playbooks, profiles, and interactions via "
            "the backend (leaves other users and agent_playbooks intact)"
        ),
    )
    cu.add_argument(
        "user_id",
        help="User id whose reflexio rows should be cleared",
    )
    cu.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm the destructive clear without prompting",
    )
    cu.set_defaults(func=cmd_clear_user)

    rs = sub.add_parser(
        "restart",
        help="Restart the reflexio backend and dashboard to pick up changes",
    )
    rs.add_argument(
        "--skip-backend",
        action="store_true",
        help="Do not stop/start the reflexio backend",
    )
    rs.add_argument(
        "--skip-dashboard",
        action="store_true",
        help="Do not stop/start the dashboard",
    )
    rs.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Skip the `npm run build` step before restarting the dashboard",
    )
    rs.set_defaults(func=cmd_restart)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] in {"install", "update", "uninstall"}:
        parser.error(f"Installation is npm-only; use `npx claude-smart {arguments[0]}`")
    args = parser.parse_args(arguments)
    env_config.load_reflexio_env()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
