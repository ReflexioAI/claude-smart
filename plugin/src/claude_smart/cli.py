"""User-facing CLI for claude-smart.

Exposes five subcommands:

- ``install``: register the GitHub marketplace and install the plugin into
  Claude Code, then seed ``~/.reflexio/.env`` with the local-provider flags.
- ``update``: update the plugin to the latest version via the native Claude
  Code plugin CLI.
- ``uninstall``: remove the plugin from Claude Code via the native plugin
  CLI. Local data under ``~/.reflexio/`` and ``~/.claude-smart/`` is left
  in place.
- ``show``: print the current project playbook and project user profiles
  (as markdown).
- ``learn``: force reflexio to run extraction on all unpublished interactions.
- ``tag "<note>"``: append a ``[correction]``-prefixed turn to the active
  session buffer so reflexio's extractor sees the signal on the next
  ``learn`` pass.
- ``restart``: stop and restart the reflexio backend + dashboard services
  (rebuilding the dashboard bundle) so local edits under the ``reflexio``
  submodule or ``plugin/dashboard/`` take effect without restarting Claude
  Code.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from claude_smart import context_format, ids, publish, state
from claude_smart.reflexio_adapter import Adapter

_REFLEXIO_ENV_PATH = Path.home() / ".reflexio" / ".env"
_DEFAULT_MARKETPLACE_SOURCE = "ReflexioAI/claude-smart"
_PLUGIN_SPEC = "claude-smart@reflexioai"
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


def cmd_show(args: argparse.Namespace) -> int:
    """Print the global playbook and this project's user profiles.

    Playbooks are fetched globally (shared across every project) so lessons
    learned anywhere are visible here. Profiles stay scoped to this project
    (resolved from cwd via ``ids.resolve_project_id``).

    Args:
        args (argparse.Namespace): Parsed CLI args. Honors ``args.project``
            (override project id for the profile fetch).

    Returns:
        int: 0 on success.
    """
    project_id = args.project or ids.resolve_project_id()
    adapter = Adapter()
    playbooks = adapter.fetch_playbooks(top_k=3)
    profiles = adapter.fetch_project_profiles(project_id, top_k=3)
    md = context_format.render(
        project_id=project_id, playbooks=playbooks, profiles=profiles
    )
    sys.stdout.write(
        md or f"_No playbook or profiles yet for project `{project_id}`._\n"
    )
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    session_id = args.session or _latest_session_id()
    if not session_id:
        sys.stdout.write("No active claude-smart session buffer found.\n")
        return 0
    project_id = args.project or ids.resolve_project_id()
    status, count = publish.publish_unpublished(
        session_id=session_id,
        project_id=project_id,
        force_extraction=True,
        skip_aggregation=True,
    )
    if status == "nothing":
        sys.stdout.write(f"Session `{session_id}`: nothing to learn from.\n")
        return 0
    if status == "ok":
        sys.stdout.write(
            f"Published {count} interactions to reflexio "
            f"(user_id={project_id}, session_id={session_id}). "
            "Extraction running.\n"
        )
        return 0
    sys.stdout.write(_REFLEXIO_UNREACHABLE_MSG)
    return 1


def cmd_tag(args: argparse.Namespace) -> int:
    session_id = args.session or _latest_session_id()
    if not session_id:
        sys.stdout.write("No active claude-smart session buffer found.\n")
        return 0
    project_id = args.project or ids.resolve_project_id(os.getcwd())
    note = args.note or "the previous answer was wrong"
    state.append(
        session_id,
        {
            "ts": int(time.time()),
            "role": "User",
            "content": f"[correction] {note}",
            "user_id": project_id,
        },
    )
    status, count = publish.publish_unpublished(
        session_id=session_id,
        project_id=project_id,
        force_extraction=True,
        skip_aggregation=True,
    )
    if status == "ok":
        sys.stdout.write(
            f"Tagged correction on session `{session_id}` and forced extraction "
            f"over {count} interactions.\n"
        )
        return 0
    if status == "nothing":
        sys.stdout.write(f"Tagged correction on session `{session_id}`.\n")
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
                sys.stdout.write(
                    "Installing dashboard dependencies (npm install, may take a minute)…\n"
                )
                try:
                    subprocess.run(
                        ["npm", "install", "--no-audit", "--no-fund"],
                        cwd=_DASHBOARD_DIR,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    sys.stderr.write(
                        f"error: npm install failed (exit {exc.returncode}); "
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
    """Delete all interactions, profiles, and user playbooks from reflexio.

    Also removes local session JSONL buffers under ``state_dir()`` so
    claude-smart starts from a clean slate. Requires ``--yes`` to proceed.

    Args:
        args (argparse.Namespace): Parsed CLI args. Honors ``args.yes``
            (skip the confirmation prompt).

    Returns:
        int: 0 on success, 1 if reflexio is unreachable or the user aborts.
    """
    if not args.yes:
        sys.stdout.write(
            "This will permanently delete ALL interactions, profiles, and "
            "user playbooks from reflexio, plus local session buffers under "
            f"{state.state_dir()}.\nRe-run with --yes to confirm.\n"
        )
        return 1

    result = Adapter().delete_all()
    if result is None:
        sys.stdout.write(_REFLEXIO_UNREACHABLE_MSG)
        return 1
    counts, errors = result

    removed_buffers = 0
    root = state.state_dir()
    if root.is_dir():
        for buf in root.glob("*.jsonl"):
            try:
                buf.unlink()
                removed_buffers += 1
            except OSError as exc:
                sys.stderr.write(f"warning: could not remove {buf}: {exc}\n")

    sys.stdout.write(
        "Cleared reflexio: "
        f"{counts.get('interactions', 0)} interactions, "
        f"{counts.get('profiles', 0)} profiles, "
        f"{counts.get('user_playbooks', 0)} user playbooks. "
        f"Removed {removed_buffers} local session buffer(s).\n"
    )
    for entity, err in errors:
        sys.stderr.write(f"warning: delete {entity} failed: {err}\n")
    return 1 if errors else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-smart")
    sub = parser.add_subparsers(dest="command", required=True)

    inst = sub.add_parser("install", help="Install claude-smart into Claude Code")
    inst.add_argument(
        "--source",
        default=_DEFAULT_MARKETPLACE_SOURCE,
        help="Marketplace ref — GitHub owner/repo, or a local directory path",
    )
    inst.set_defaults(func=cmd_install)

    upd = sub.add_parser("update", help="Update claude-smart to the latest version")
    upd.set_defaults(func=cmd_update)

    uni = sub.add_parser("uninstall", help="Remove claude-smart from Claude Code")
    uni.set_defaults(func=cmd_uninstall)

    sh = sub.add_parser(
        "show",
        help="Show the current project playbook and project user profiles",
    )
    sh.add_argument("--project", help="Override project id")
    sh.set_defaults(func=cmd_show)

    ln = sub.add_parser("learn", help="Force reflexio extraction now")
    ln.add_argument("--project", help="Override project id")
    ln.add_argument("--session", help="Session id (defaults to latest)")
    ln.set_defaults(func=cmd_learn)

    tg = sub.add_parser("tag", help="Tag the current session with a correction note")
    tg.add_argument("note", nargs="?", default="", help="Correction description")
    tg.add_argument("--session", help="Session id (defaults to latest)")
    tg.add_argument("--project", help="Override project id")
    tg.set_defaults(func=cmd_tag)

    ca = sub.add_parser(
        "clear-all",
        help="Delete all interactions, profiles, and user playbooks from reflexio",
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
