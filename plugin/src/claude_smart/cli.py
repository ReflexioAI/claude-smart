"""User-facing CLI for claude-smart.

Exposes the following subcommands:

- ``install``: register the GitHub marketplace and install the plugin into
  Claude Code, then seed ``~/.reflexio/.env`` with the local-provider flags.
- ``update``: update the plugin to the latest version via the native Claude
  Code plugin CLI.
- ``uninstall``: remove the plugin from Claude Code via the native plugin
  CLI. Local data under ``~/.reflexio/`` and ``~/.claude-smart/`` is left
  in place.
- ``show``: print current project-specific skills and project preferences
  (as markdown).
- ``learn``: publish unpublished interactions and force reflexio
  extraction now over the active session buffer.
- ``restart``: stop and restart the reflexio backend + dashboard services
  (rebuilding the dashboard bundle) so local edits under the ``reflexio``
  submodule or ``plugin/dashboard/`` take effect without restarting Claude
  Code.
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
from dataclasses import dataclass
from pathlib import Path

from claude_smart import context_format, ids, publish, state
from claude_smart.reflexio_adapter import Adapter

_REFLEXIO_ENV_PATH = Path.home() / ".reflexio" / ".env"
_DEFAULT_MARKETPLACE_SOURCE = "ReflexioAI/claude-smart"
_PLUGIN_SPEC = "claude-smart@reflexioai"
_CODEX_MARKETPLACE_NAME = "reflexioai"
_CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
_CODEX_CLI_TIMEOUT_SECONDS = 30
_REFLEXIO_UNREACHABLE_MSG = (
    "Failed to reach reflexio. Check ~/.claude-smart/backend.log "
    "or restart Claude Code.\n"
)

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _THIS_DIR.parents[1]  # plugin/src/claude_smart/ -> plugin/
_REPO_ROOT = _PLUGIN_ROOT.parent
_SCRIPTS_DIR = _PLUGIN_ROOT / "scripts"
_DASHBOARD_DIR = _PLUGIN_ROOT / "dashboard"
_BACKEND_SCRIPT = _SCRIPTS_DIR / "backend-service.sh"
_DASHBOARD_SCRIPT = _SCRIPTS_DIR / "dashboard-service.sh"
_REFLEXIO_DIR = Path.home() / ".reflexio"
_DEFAULT_STORAGE_ROOT = _REFLEXIO_DIR / "data"
_REFLEXIO_CONFIG_PATH = _REFLEXIO_DIR / "configs" / "config_self-host-org.json"
_LOCAL_STORAGE_ENV = "LOCAL_STORAGE_PATH"
_CODEX_REQUIRED_FILES = (
    Path(".agents/plugins/marketplace.json"),
    Path("plugin/.codex-plugin/plugin.json"),
    Path("plugin/hooks/codex-hooks.json"),
    Path("plugin/scripts/_codex_env.sh"),
)


def _latest_session_id() -> str | None:
    """Most-recently-modified session JSONL in the state dir. None if none exist."""
    root = state.state_dir()
    if not root.is_dir():
        return None
    files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    return files[0].stem


def _seed_reflexio_env() -> list[str]:
    """Append the two local-provider flags to ``~/.reflexio/.env``, idempotently.

    Returns:
        list[str]: Flag names that were newly appended (empty if already present).
    """
    _REFLEXIO_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REFLEXIO_ENV_PATH.touch(exist_ok=True)
    existing = _REFLEXIO_ENV_PATH.read_text()
    flags = ("CLAUDE_SMART_USE_LOCAL_CLI", "CLAUDE_SMART_USE_LOCAL_EMBEDDING")
    missing = [f for f in flags if f"{f}=" not in existing]
    if not missing:
        return []
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with _REFLEXIO_ENV_PATH.open("a") as fh:
        fh.write(prefix + "\n".join(f"{f}=1" for f in missing) + "\n")
    return missing


def _missing_codex_marketplace_files(root: Path) -> list[Path]:
    return [entry for entry in _CODEX_REQUIRED_FILES if not (root / entry).is_file()]


def _run_codex(args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["codex", *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_CODEX_CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            command,
            124,
            "",
            f"Codex CLI timed out after {_CODEX_CLI_TIMEOUT_SECONDS}s: {' '.join(command)}",
        )


def _set_toml_feature(path: Path, feature: str, value: bool) -> bool:
    """Set one boolean under ``[features]`` in a minimal TOML file."""
    desired = f"{feature} = {'true' if value else 'false'}"
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    feature_re = re.compile(rf"^\s*{re.escape(feature)}\s*=")
    try:
        text = path.read_text() if path.exists() else ""
    except OSError:
        return False

    lines = text.splitlines()
    in_features = False
    features_idx: int | None = None
    insert_idx: int | None = None
    changed = False
    out: list[str] = []

    for line in lines:
        section_match = section_re.match(line)
        if section_match:
            if in_features and insert_idx is None:
                insert_idx = len(out)
            in_features = section_match.group(1).strip() == "features"
            if in_features:
                features_idx = len(out)
            out.append(line)
            continue
        if in_features and feature_re.match(line):
            out.append(desired)
            changed = changed or line != desired
            continue
        out.append(line)

    if features_idx is None:
        if out and out[-1].strip():
            out.append("")
        out.extend(["[features]", desired])
        changed = True
    else:
        section_end = insert_idx if insert_idx is not None else len(out)
        if not any(
            feature_re.match(line) for line in out[features_idx + 1 : section_end]
        ):
            idx = insert_idx if insert_idx is not None else len(out)
            out.insert(idx, desired)
            changed = True

    if not changed and text.endswith("\n"):
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n")
    except OSError:
        return False
    return True


def _enable_codex_plugin_hooks() -> tuple[bool, str]:
    result = _run_codex(["features", "enable", "plugin_hooks"])
    if result.returncode == 0:
        return True, "codex features enable plugin_hooks"
    if _set_toml_feature(_CODEX_CONFIG_PATH, "plugin_hooks", True):
        return True, f"set plugin_hooks = true in {_CODEX_CONFIG_PATH}"
    return False, (
        result.stderr or result.stdout or "could not update Codex config"
    ).strip()


def _register_codex_marketplace(root: Path) -> tuple[bool, str]:
    result = _run_codex(["plugin", "marketplace", "add", str(root)])
    if result.returncode == 0:
        upgrade = _run_codex(
            ["plugin", "marketplace", "upgrade", _CODEX_MARKETPLACE_NAME]
        )
        suffix = " and refreshed cache" if upgrade.returncode == 0 else ""
        return True, f"registered Codex marketplace{suffix}"
    output = (result.stderr or result.stdout or "").strip()
    if "different source" in output:
        removed = _run_codex(
            ["plugin", "marketplace", "remove", _CODEX_MARKETPLACE_NAME]
        )
        if removed.returncode == 0:
            added = _run_codex(["plugin", "marketplace", "add", str(root)])
            if added.returncode == 0:
                return True, "replaced stale Codex marketplace registration"
    return False, output or "Codex CLI does not expose plugin marketplace commands"


def cmd_install_codex(_args: argparse.Namespace) -> int:
    """Install the local claude-smart plugin marketplace for Codex."""
    if not shutil.which("codex"):
        sys.stderr.write("error: 'codex' CLI not found on PATH. Install Codex first.\n")
        return 1
    if not shutil.which("uv"):
        sys.stderr.write(
            "error: 'uv' not found on PATH. Install uv or restart your shell.\n"
        )
        return 1

    missing = _missing_codex_marketplace_files(_REPO_ROOT)
    if missing:
        formatted = ", ".join(str(path) for path in missing)
        sys.stderr.write(f"error: Codex marketplace files missing: {formatted}\n")
        return 1

    added = _seed_reflexio_env()
    if added:
        sys.stdout.write(f"Seeded {_REFLEXIO_ENV_PATH} with {', '.join(added)}.\n")

    hooks_ok, hooks_msg = _enable_codex_plugin_hooks()
    if hooks_ok:
        sys.stdout.write(f"Enabled Codex plugin hooks ({hooks_msg}).\n")
    else:
        sys.stderr.write(f"warning: could not enable Codex plugin hooks: {hooks_msg}\n")

    registered, registration_msg = _register_codex_marketplace(_REPO_ROOT)
    if registered:
        sys.stdout.write(f"{registration_msg}.\n")
    else:
        sys.stderr.write(f"warning: {registration_msg}\n")

    if registered:
        sys.stdout.write(
            "\nclaude-smart Codex support is prepared.\n"
            "Open Codex in this repo, run /plugins, install claude-smart from "
            "the claude-smart (local) marketplace if it is not already installed, "
            "then restart Codex so hooks reload. Uninstall removes the marketplace "
            "registration but leaves shared claude-smart data and Codex's global "
            "plugin_hooks feature intact.\n"
        )
    else:
        sys.stdout.write(
            "\nCodex hooks enabled; marketplace registration failed.\n"
            f"Install manually with `codex plugin marketplace add {_REPO_ROOT}`, "
            "then open Codex, run /plugins, install claude-smart from the "
            "claude-smart (local) marketplace, and restart Codex so hooks reload.\n"
        )
    return 0 if hooks_ok and registered else 1


def cmd_install(args: argparse.Namespace) -> int:
    """Install claude-smart into Claude Code via the native plugin CLI.

    Runs ``claude plugin marketplace add`` followed by ``claude plugin install``,
    then appends the local-provider flags to ``~/.reflexio/.env`` so reflexio
    can route generation through the local Claude Code CLI.

    Args:
        args (argparse.Namespace): Parsed CLI args. Uses ``args.source`` as the
            marketplace ref (``owner/repo`` on GitHub, or a local directory).

    Returns:
        int: 0 on success, non-zero if the ``claude`` CLI is missing or fails.
    """
    if getattr(args, "host", "claude-code") == "codex":
        return cmd_install_codex(args)

    if not shutil.which("claude"):
        sys.stderr.write(
            "error: 'claude' CLI not found on PATH. "
            "Install Claude Code first: https://claude.com/claude-code\n"
        )
        return 1

    for cmd in (
        ["claude", "plugin", "marketplace", "add", args.source],
        ["claude", "plugin", "install", _PLUGIN_SPEC],
    ):
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            sys.stderr.write(f"error: {' '.join(cmd)} failed (exit {exc.returncode})\n")
            return exc.returncode or 1

    added = _seed_reflexio_env()
    if added:
        sys.stdout.write(f"Seeded {_REFLEXIO_ENV_PATH} with {', '.join(added)}.\n")

    sys.stdout.write("\nclaude-smart installed. Restart Claude Code in your project.\n")
    return 0


def cmd_update(_args: argparse.Namespace) -> int:
    """Update claude-smart to the latest version via the native plugin CLI.

    Runs ``claude plugin update claude-smart@reflexioai``.

    Args:
        args (argparse.Namespace): Parsed CLI args (unused).

    Returns:
        int: 0 on success, non-zero if the ``claude`` CLI is missing or fails.
    """
    if not shutil.which("claude"):
        sys.stderr.write(
            "error: 'claude' CLI not found on PATH. "
            "Install Claude Code first: https://claude.com/claude-code\n"
        )
        return 1

    cmd = ["claude", "plugin", "update", _PLUGIN_SPEC]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"error: {' '.join(cmd)} failed (exit {exc.returncode})\n")
        return exc.returncode or 1

    sys.stdout.write("\nclaude-smart updated. Restart Claude Code to apply.\n")
    return 0


def cmd_uninstall(_args: argparse.Namespace) -> int:
    """Uninstall claude-smart from Claude Code via the native plugin CLI.

    Runs ``claude plugin uninstall claude-smart@reflexioai``. Local data under
    ``~/.reflexio/`` and ``~/.claude-smart/`` is left in place.

    Args:
        args (argparse.Namespace): Parsed CLI args (unused).

    Returns:
        int: 0 on success, non-zero if the ``claude`` CLI is missing or fails.
    """
    if getattr(_args, "host", "claude-code") == "codex":
        return cmd_uninstall_codex(_args)

    if not shutil.which("claude"):
        sys.stderr.write(
            "error: 'claude' CLI not found on PATH. "
            "Install Claude Code first: https://claude.com/claude-code\n"
        )
        return 1

    cmd = ["claude", "plugin", "uninstall", _PLUGIN_SPEC]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"error: {' '.join(cmd)} failed (exit {exc.returncode})\n")
        return exc.returncode or 1

    sys.stdout.write(
        "\nclaude-smart uninstalled. Restart Claude Code to apply.\n"
        "Local data in ~/.reflexio/ and ~/.claude-smart/ was left in place — "
        "remove manually if desired.\n"
    )
    return 0


def cmd_uninstall_codex(_args: argparse.Namespace) -> int:
    if not shutil.which("codex"):
        sys.stdout.write("Codex CLI not found; skipping marketplace removal.\n")
        return 0
    result = _run_codex(["plugin", "marketplace", "remove", _CODEX_MARKETPLACE_NAME])
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        sys.stderr.write(
            "warning: Codex marketplace removal failed"
            + (f": {output}" if output else "")
            + "\n"
        )
        return 1
    sys.stdout.write(
        "claude-smart Codex marketplace removed. Restart Codex to apply. "
        "Codex's global plugin_hooks feature and local data under ~/.reflexio "
        "and ~/.claude-smart were left in place.\n"
    )
    return 0


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
    project_id = args.project or ids.resolve_project_id()
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
    sys.stdout.write(
        md or f"_No skills or preferences yet for project `{project_id}`._\n"
    )
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    """Force reflexio extraction over the active session's interactions.

    Publishes unpublished interactions with ``force_extraction=True`` so
    extraction runs immediately rather than at the next batch interval.
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
            publish), 1 if reflexio is unreachable.
    """
    session_id = args.session or _latest_session_id()
    if not session_id:
        sys.stdout.write("No active claude-smart session buffer found.\n")
        return 0
    project_id = args.project or ids.resolve_project_id()

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
        skip_aggregation=False,
    )
    if status == "ok":
        suffix = " (including your note)" if note else ""
        sys.stdout.write(
            f"Forced extraction on session `{session_id}` over {count} interactions{suffix}.\n"
        )
        return 0
    if status == "nothing":
        sys.stdout.write(f"No unpublished interactions on session `{session_id}`.\n")
        return 0
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
    try:
        subprocess.run([str(script), subcmd], check=True)
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
    deadline = time.monotonic() + wait_ready_s
    while True:
        result = subprocess.run(
            [str(script), "status"], capture_output=True, text=True, check=False
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


def _read_dotenv_value(env_path: Path, key: str) -> str | None:
    """Read one simple KEY=VALUE binding from a dotenv file."""
    if not env_path.is_file():
        return None
    try:
        lines = env_path.read_text().splitlines()
    except OSError:
        return None
    prefix = f"{key}="
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return None


def _resolve_absolute_path(raw_path: str | Path, *, source: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise _ClearAllError(f"{source} must be an absolute path: {raw_path}")
    return path


def _effective_storage_root() -> Path:
    raw = os.environ.get(_LOCAL_STORAGE_ENV, "").strip()
    if not raw:
        raw = _read_dotenv_value(_REFLEXIO_ENV_PATH, _LOCAL_STORAGE_ENV) or ""
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
    targets: list[_ClearAllTarget] = []
    for child in sorted(base_dir.glob("disk_*")):
        targets.append(_ClearAllTarget(child, "dir", "disk org data"))
    return targets


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
                for suffix in ("", "-wal", "-shm", "-journal"):
                    targets.append(
                        _ClearAllTarget(
                            Path(f"{db_path}{suffix}"),
                            "file",
                            "configured SQLite data",
                        )
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
    on the ``reflexio`` submodule or the dashboard.

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
        elif not shutil.which("npm"):
            sys.stderr.write("warning: npm not on PATH; serving previous build\n")
        else:
            next_bin = _DASHBOARD_DIR / "node_modules" / ".bin" / "next"
            if not next_bin.exists():
                install_cmd = (
                    ["npm", "ci", "--no-audit", "--no-fund"]
                    if (_DASHBOARD_DIR / "package-lock.json").exists()
                    else ["npm", "install", "--no-audit", "--no-fund"]
                )
                install_label = " ".join(install_cmd)
                sys.stdout.write(
                    "Installing dashboard dependencies "
                    f"({install_label}, may take a minute)…\n"
                )
                try:
                    subprocess.run(
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
                subprocess.run(["npm", "run", "build"], cwd=_DASHBOARD_DIR, check=True)
            except subprocess.CalledProcessError as exc:
                sys.stderr.write(
                    f"error: dashboard build failed (exit {exc.returncode}); "
                    "not starting dashboard.\n"
                )
                if do_backend:
                    sys.stdout.write("Starting reflexio backend…\n")
                    _run_service(_BACKEND_SCRIPT, "start")
                    sys.stdout.write(
                        f"reflexio backend: {_service_status(_BACKEND_SCRIPT)}\n"
                    )
                return exc.returncode or 1

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

    was_running = _is_running_status(_service_status(_BACKEND_SCRIPT, wait_ready_s=0.0))
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-smart")
    sub = parser.add_subparsers(dest="command", required=True)

    inst = sub.add_parser("install", help="Install claude-smart")
    inst.add_argument(
        "--host",
        choices=("claude-code", "codex"),
        default="claude-code",
        help="Install target host",
    )
    inst.add_argument(
        "--source",
        default=_DEFAULT_MARKETPLACE_SOURCE,
        help="Marketplace ref — GitHub owner/repo, or a local directory path",
    )
    inst.set_defaults(func=cmd_install)

    upd = sub.add_parser("update", help="Update claude-smart to the latest version")
    upd.set_defaults(func=cmd_update)

    uni = sub.add_parser("uninstall", help="Remove claude-smart")
    uni.add_argument(
        "--host",
        choices=("claude-code", "codex"),
        default="claude-code",
        help="Uninstall target host",
    )
    uni.set_defaults(func=cmd_uninstall)

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
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
