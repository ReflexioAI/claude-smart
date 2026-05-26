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
  (rebuilding the dashboard bundle) so local Reflexio checkout edits or
  ``plugin/dashboard/`` edits take effect without restarting Claude Code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from claude_smart import context_format, env_config, ids, publish, state
from claude_smart.reflexio_adapter import Adapter

_REFLEXIO_ENV_PATH = env_config.REFLEXIO_ENV_PATH
_MANAGED_REFLEXIO_URL = env_config.MANAGED_REFLEXIO_URL
_DEFAULT_MARKETPLACE_SOURCE = "ReflexioAI/claude-smart"
_PLUGIN_SPEC = "claude-smart@reflexioai"
_CODEX_MARKETPLACE_NAME = "reflexioai"
_CODEX_MARKETPLACE_DISPLAY_NAME = "ReflexioAI"
_CODEX_PLUGIN_ID = f"claude-smart@{_CODEX_MARKETPLACE_NAME}"
_CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
_CODEX_LOCAL_MARKETPLACE_ROOT = (
    Path.home() / ".claude" / "plugins" / "marketplaces" / _CODEX_MARKETPLACE_NAME
)
_CODEX_LOCAL_PLUGIN_PATH = Path("plugins") / "claude-smart"
_CODEX_PLUGIN_CACHE_DIR = (
    Path.home()
    / ".codex"
    / "plugins"
    / "cache"
    / _CODEX_MARKETPLACE_NAME
    / "claude-smart"
)
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
_STATE_DIR = Path.home() / ".claude-smart"
_LOCAL_DATA_NOTICE = (
    "Local data was kept so reinstalling claude-smart can reuse your learned "
    "rules, sessions, logs, and local Reflexio data.\n"
    "Kept folders:\n"
    "  ~/.claude-smart\n"
    "  ~/.reflexio\n"
    "Delete them only if you want a full reset or need to remove local "
    "claude-smart data from this machine:\n"
    "  rm -rf ~/.claude-smart ~/.reflexio\n"
)
_INSTALL_FAILURE_MARKER = _STATE_DIR / "install-failed"
_SERVICE_STATUS_PROBE_FAILED = "probe_failed"
_DEFAULT_STORAGE_ROOT = _REFLEXIO_DIR / "data"
_REFLEXIO_CONFIG_PATH = _REFLEXIO_DIR / "configs" / "config_self-host-org.json"
_LOCAL_STORAGE_ENV = "LOCAL_STORAGE_PATH"
_CODEX_REQUIRED_FILES = (
    Path(".agents/plugins/marketplace.json"),
    Path("plugin/.codex-plugin/plugin.json"),
    Path("plugin/hooks/codex-hooks.json"),
    Path("plugin/scripts/codex-claude-compat"),
    Path("plugin/scripts/codex-claude-compat.cmd"),
    Path("plugin/scripts/codex-claude-compat.js"),
    Path("plugin/scripts/codex-hook.js"),
    Path("plugin/scripts/backend-log-runner.sh"),
    Path("plugin/scripts/_codex_env.sh"),
)
_COPYTREE_IGNORE = shutil.ignore_patterns(
    ".venv",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    "node_modules",
)


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
    expanded = os.path.expandvars(os.path.expanduser(candidate))
    if os.path.isfile(expanded):
        return expanded
    return shutil.which(expanded)


def _iter_path_candidates(names: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir:
            continue
        directory = os.path.expandvars(os.path.expanduser(raw_dir))
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
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


def _semver_tuple(path: Path) -> tuple[int, int, int] | None:
    parts = path.name.split(".", 2)
    if len(parts) != 3:
        return None
    try:
        patch = parts[2].split("-", 1)[0].split("+", 1)[0]
        return (int(parts[0]), int(parts[1]), int(patch))
    except ValueError:
        return None


def _installed_plugin_sort_key(path: Path) -> tuple[int, int, int, int, float]:
    version = _semver_tuple(path)
    if version is not None:
        return (1, *version, path.stat().st_mtime)
    return (0, 0, 0, 0, path.stat().st_mtime)


def _find_claude_code_plugin_root() -> Path | None:
    """Locate the installed Claude Code plugin root after native install."""
    cache_root = (
        Path.home()
        / ".claude"
        / "plugins"
        / "cache"
        / _CODEX_MARKETPLACE_NAME
        / "claude-smart"
    )
    candidates: list[Path] = []
    if cache_root.is_dir():
        for child in cache_root.iterdir():
            if (
                child.is_dir()
                and (child / "pyproject.toml").is_file()
                and (child / "scripts" / "smart-install.sh").is_file()
            ):
                candidates.append(child)
    candidates.sort(key=_installed_plugin_sort_key, reverse=True)
    candidates.extend(
        [
            Path.home()
            / ".claude"
            / "plugins"
            / "marketplaces"
            / _CODEX_MARKETPLACE_NAME
            / "plugin",
            _PLUGIN_ROOT,
        ]
    )
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "scripts" / "smart-install.sh"
        ).is_file():
            return candidate
    return None


def _force_plugin_root(plugin_root: Path) -> None:
    """Point ~/.reflexio/plugin-root at the installed plugin root."""
    _REFLEXIO_DIR.mkdir(parents=True, exist_ok=True)
    link = _REFLEXIO_DIR / "plugin-root"
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.exists():
        raise OSError(f"refusing to replace non-symlink plugin-root at {link}")
    try:
        link.symlink_to(plugin_root, target_is_directory=True)
    except OSError:
        if link.exists():
            raise
        (_REFLEXIO_DIR / "plugin-root.txt").write_text(f"{plugin_root}\n")


def _bootstrap_claude_code_install() -> tuple[bool, str]:
    """Run smart-install immediately for the installed Claude Code plugin."""
    plugin_root = _find_claude_code_plugin_root()
    if plugin_root is None:
        return False, "could not locate installed Claude Code plugin root after install"
    try:
        _force_plugin_root(plugin_root)
    except OSError as exc:
        return False, str(exc)
    bash = _resolve_bash()
    if not bash:
        return False, "bash is required to bootstrap claude-smart dependencies"
    result = subprocess.run(
        [bash, str(plugin_root / "scripts" / "smart-install.sh")],
        cwd=plugin_root,
    )
    if result.returncode != 0:
        return False, f"smart-install.sh failed in {plugin_root}"
    if _INSTALL_FAILURE_MARKER.is_file():
        first_line = _INSTALL_FAILURE_MARKER.read_text().splitlines()
        reason = (first_line[0].strip() if first_line else "") or "unknown error"
        return False, reason
    return True, str(plugin_root)


def _missing_codex_marketplace_files(root: Path) -> list[Path]:
    return [entry for entry in _CODEX_REQUIRED_FILES if not (root / entry).is_file()]


def _prepare_codex_local_marketplace() -> Path:
    """Create the local Codex marketplace wrapper used for install.

    Wipes any prior wrapper at ``_CODEX_LOCAL_MARKETPLACE_ROOT``, copies
    this checkout's ``plugin/`` directory under ``plugins/claude-smart``
    (skipping dev artifacts in ``_COPYTREE_IGNORE``), and writes a fresh
    ``.agents/plugins/marketplace.json`` pointing at it. The directory
    name ``plugins/claude-smart`` keeps the on-disk plugin folder aligned
    with the manifest name so Codex resolves it cleanly.

    Returns:
        Path: The marketplace root that Codex should register.
    """
    marketplace_root = _CODEX_LOCAL_MARKETPLACE_ROOT
    marketplace_manifest = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    plugin_dir = marketplace_root / _CODEX_LOCAL_PLUGIN_PATH

    if marketplace_root.exists() or marketplace_root.is_symlink():
        if marketplace_root.is_dir() and not marketplace_root.is_symlink():
            shutil.rmtree(marketplace_root)
        else:
            marketplace_root.unlink()
    marketplace_manifest.parent.mkdir(parents=True, exist_ok=True)
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_PLUGIN_ROOT, plugin_dir, symlinks=False, ignore=_COPYTREE_IGNORE)

    marketplace_manifest.write_text(
        json.dumps(
            {
                "name": _CODEX_MARKETPLACE_NAME,
                "interface": {"displayName": _CODEX_MARKETPLACE_DISPLAY_NAME},
                "plugins": [
                    {
                        "name": "claude-smart",
                        "source": {
                            "source": "local",
                            "path": f"./{_CODEX_LOCAL_PLUGIN_PATH.as_posix()}",
                        },
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                        "category": "Productivity",
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return marketplace_root


def _command_is_publish_hook(command: object) -> bool:
    if not isinstance(command, str):
        return False
    return bool(
        re.search(
            r"hook_entry\.sh\b[\s\"']+(?:codex|claude-code)[\s\"']+(?:stop|session-end)\b",
            command,
        )
        or re.search(
            r"codex-hook\.js\"?(?:\s+\"?hook\"?){1}\s+\"?(?:stop|session-end)\"?",
            command,
        )
    )


def _prune_publish_hooks_for_read_only(plugin_root: Path) -> None:
    """Remove publish-to-reflexio hook commands from a copied/installed plugin."""
    for hook_file in ("hooks.json", "codex-hooks.json"):
        hook_path = plugin_root / "hooks" / hook_file
        if not hook_path.is_file():
            continue
        parsed = json.loads(hook_path.read_text())
        hooks_by_event = parsed.get("hooks")
        if not isinstance(hooks_by_event, dict):
            continue
        for event in list(hooks_by_event):
            blocks = []
            for block in hooks_by_event.get(event) or []:
                if not isinstance(block, dict):
                    continue
                kept_hooks = [
                    hook
                    for hook in block.get("hooks") or []
                    if not _command_is_publish_hook(
                        hook.get("command") if isinstance(hook, dict) else None
                    )
                ]
                if kept_hooks:
                    next_block = dict(block)
                    next_block["hooks"] = kept_hooks
                    blocks.append(next_block)
            if blocks:
                hooks_by_event[event] = blocks
            else:
                del hooks_by_event[event]
        hook_path.write_text(json.dumps(parsed, indent=2) + "\n")


def _restore_publish_hooks_from_source(plugin_root: Path) -> None:
    """Restore full hook manifests before applying the current read-only state."""
    source_hooks_dir = _PLUGIN_ROOT / "hooks"
    target_hooks_dir = plugin_root / "hooks"
    for hook_file in ("hooks.json", "codex-hooks.json"):
        source_path = source_hooks_dir / hook_file
        target_path = target_hooks_dir / hook_file
        if (
            source_path.is_file()
            and target_path.is_file()
            and source_path.resolve() != target_path.resolve()
        ):
            shutil.copyfile(source_path, target_path)


def _run_codex(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke the ``codex`` CLI with a hard timeout.

    Args:
        args: Argument list after ``codex`` (e.g. ``["features", "enable",
            "plugin_hooks"]``).

    Returns:
        subprocess.CompletedProcess[str]: The completed run. On timeout,
            returns a synthetic process with exit code 124 and a stderr
            message identifying the hung command.
    """
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


def _remove_toml_sections(
    path: Path, *, exact: set[str], prefixes: tuple[str, ...] = ()
) -> bool:
    """Remove simple top-level TOML sections by section name.

    Walks ``path`` line by line, dropping every line from a matching
    ``[section]`` header through the next header. Intended for minimal
    TOML files like ``~/.codex/config.toml``; nested array-of-tables
    (``[[…]]``) and inline tables are not supported.

    Args:
        path: TOML file to rewrite. A missing file is treated as success.
        exact: Section names to drop (e.g. ``"marketplaces.reflexioai"``).
        prefixes: Section-name prefixes to drop (e.g.
            ``("hooks.state.\"claude-smart@reflexioai:",)``).

    Returns:
        bool: ``True`` if the file is now consistent (including the
            no-op case). ``False`` only if the file existed but could
            not be read or written.
    """
    try:
        text = path.read_text() if path.exists() else ""
    except OSError:
        return False
    if not text:
        return True

    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    changed = False
    dropping = False
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        section_match = section_re.match(line)
        if section_match:
            name = section_match.group(1).strip()
            dropping = name in exact or any(
                name.startswith(prefix) for prefix in prefixes
            )
            changed = changed or dropping
        if not dropping:
            out.append(line)

    if not changed:
        return True
    try:
        path.write_text("".join(out))
    except OSError:
        return False
    return True


def _set_codex_plugin_enabled(path: Path) -> bool:
    """Write Codex's installed-plugin config section for claude-smart."""
    section_name = f'plugins."{_CODEX_PLUGIN_ID}"'
    if not _remove_toml_sections(path, exact={section_name}):
        return False
    try:
        text = path.read_text() if path.exists() else ""
    except OSError:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    if text.strip():
        text += "\n"
    text += f"[{section_name}]\nenabled = true\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError:
        return False
    return True


def _toml_dotted_quoted(name: str) -> str:
    """Quote a TOML bare-key segment whose name may contain ``"`` or ``\\``."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _set_codex_hook_states(path: Path, states: dict[str, str]) -> bool:
    """Trust and enable Codex hook state entries for the given hook hashes."""
    if not states:
        return False
    if not _remove_toml_sections(
        path,
        exact=set(),
        prefixes=(f'hooks.state."{_CODEX_PLUGIN_ID}:',),
    ):
        return False
    try:
        text = path.read_text() if path.exists() else ""
    except OSError:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    if "[hooks.state]" not in text:
        if text.strip():
            text += "\n"
        text += "[hooks.state]\n"
    if text.strip():
        text += "\n"
    for key, current_hash in sorted(states.items()):
        text += (
            f"[hooks.state.{_toml_dotted_quoted(key)}]\n"
            "enabled = true\n"
            f'trusted_hash = "{current_hash}"\n\n'
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.rstrip() + "\n")
    except OSError:
        return False
    return True


def _read_codex_app_server_response(
    proc: subprocess.Popen[str], response_id: int, deadline: float
) -> dict[str, object]:
    """Read JSON-RPC lines from ``proc.stdout`` until ``response_id`` arrives.

    Skips unrelated notifications and non-JSON output. Raises ``RuntimeError``
    if the child exits, ``TimeoutError`` once ``deadline`` is reached, or
    re-raises Codex's own error payload as ``RuntimeError``.
    """
    if proc.stdout is None:
        raise RuntimeError("Codex app-server stdout pipe is not available")
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            if proc.poll() is not None:
                raise RuntimeError("Codex app-server exited before responding")
            continue
        line = proc.stdout.readline()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == response_id:
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            return message
    raise TimeoutError("Codex app-server did not respond in time")


def _codex_app_server_request(
    proc: subprocess.Popen[str],
    response_id: int,
    method: str,
    params: dict[str, object],
    deadline: float,
) -> dict[str, object]:
    """Send one JSON-RPC request to the Codex app-server and await the response."""
    if proc.stdin is None:
        raise RuntimeError("Codex app-server stdin pipe is not available")
    proc.stdin.write(
        json.dumps({"id": response_id, "method": method, "params": params})
    )
    proc.stdin.write("\n")
    proc.stdin.flush()
    return _read_codex_app_server_response(proc, response_id, deadline)


def _list_codex_plugin_hooks(cwd: Path) -> tuple[bool, list[dict[str, object]], str]:
    """Ask Codex for discovered hook metadata, including current trust hashes."""
    try:
        popen = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        return False, [], f"could not start Codex app-server: {exc}"

    proc = popen
    try:
        deadline = time.monotonic() + _CODEX_CLI_TIMEOUT_SECONDS
        _codex_app_server_request(
            proc,
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "claude_smart_installer",
                    "title": "claude-smart installer",
                    "version": "0.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            deadline,
        )
        if proc.stdin is None:
            return False, [], "Codex app-server stdin pipe is not available"
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        response = _codex_app_server_request(
            proc,
            2,
            "hooks/list",
            {"cwds": [str(cwd)]},
            deadline,
        )
    except (OSError, RuntimeError, TimeoutError) as exc:
        return False, [], str(exc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    result = response.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or not data:
        return False, [], "Codex app-server returned no hook metadata"
    hooks = data[0].get("hooks") if isinstance(data[0], dict) else None
    if not isinstance(hooks, list):
        return False, [], "Codex app-server hook metadata was malformed"
    plugin_hooks = [
        hook
        for hook in hooks
        if isinstance(hook, dict)
        and (
            hook.get("pluginId") == _CODEX_PLUGIN_ID
            or str(hook.get("key", "")).startswith(f"{_CODEX_PLUGIN_ID}:")
        )
    ]
    return True, plugin_hooks, f"found {len(plugin_hooks)} claude-smart hooks"


def _trust_codex_plugin_hooks(cwd: Path) -> tuple[bool, str]:
    """Seed Codex per-hook trust state for the installed claude-smart plugin."""
    ok, hooks, message = _list_codex_plugin_hooks(cwd)
    if not ok:
        return False, message
    states: dict[str, str] = {}
    for hook in hooks:
        key = hook.get("key")
        current_hash = hook.get("currentHash")
        if (
            isinstance(key, str)
            and key.startswith(f"{_CODEX_PLUGIN_ID}:")
            and isinstance(current_hash, str)
        ):
            states[key] = current_hash
    if not states:
        return False, "Codex did not report trust hashes for claude-smart hooks"
    if not _set_codex_hook_states(_CODEX_CONFIG_PATH, states):
        return (
            False,
            f"could not write claude-smart hook trust state to {_CODEX_CONFIG_PATH}",
        )
    return True, f"trusted and enabled {len(states)} claude-smart Codex hooks"


def _codex_plugin_version(plugin_root: Path) -> str | None:
    try:
        manifest = json.loads(
            (plugin_root / ".codex-plugin" / "plugin.json").read_text()
        )
    except (OSError, json.JSONDecodeError):
        return None
    version = manifest.get("version")
    return version if isinstance(version, str) and version else None


def _install_codex_plugin_cache(plugin_root: Path) -> tuple[bool, str]:
    """Best-effort local install equivalent to selecting Install in /plugins."""
    version = _codex_plugin_version(plugin_root)
    if not version:
        return (
            False,
            f"missing version in {plugin_root / '.codex-plugin' / 'plugin.json'}",
        )
    cache_dir = _CODEX_PLUGIN_CACHE_DIR / version
    try:
        shutil.rmtree(cache_dir, ignore_errors=True)
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(plugin_root, cache_dir, symlinks=False, ignore=_COPYTREE_IGNORE)
    except OSError as exc:
        return False, f"could not write Codex plugin cache: {exc}"
    if not _set_codex_plugin_enabled(_CODEX_CONFIG_PATH):
        return False, f"could not enable {_CODEX_PLUGIN_ID} in {_CODEX_CONFIG_PATH}"
    return True, f"installed Codex plugin cache at {cache_dir}"


def _cleanup_codex_install_state() -> bool:
    """Remove Codex's local install artifacts while preserving shared learning data.

    Drops the ``[plugins."claude-smart@reflexioai"]`` /
    ``[marketplaces.reflexioai]`` / ``[hooks.state."claude-smart@reflexioai:…"]``
    sections from ``~/.codex/config.toml``, removes the marketplace
    wrapper and the Codex-side plugin cache, and tries to clean the
    parent cache directory if it is now empty. Shared
    ``~/.reflexio/`` and ``~/.claude-smart/`` data, and Codex's global
    ``plugin_hooks`` feature, are intentionally left in place.

    Returns:
        bool: ``True`` if the TOML rewrite succeeded (or no rewrite was
            needed). Filesystem cleanup errors are swallowed.
    """
    ok = _remove_toml_sections(
        _CODEX_CONFIG_PATH,
        exact={
            f'plugins."{_CODEX_PLUGIN_ID}"',
            f"marketplaces.{_CODEX_MARKETPLACE_NAME}",
        },
        prefixes=(f'hooks.state."{_CODEX_PLUGIN_ID}:',),
    )
    shutil.rmtree(_CODEX_LOCAL_MARKETPLACE_ROOT, ignore_errors=True)
    shutil.rmtree(_CODEX_PLUGIN_CACHE_DIR, ignore_errors=True)
    try:
        _CODEX_PLUGIN_CACHE_DIR.parent.rmdir()
    except OSError:
        pass
    return ok


def _enable_codex_plugin_hooks() -> tuple[bool, str]:
    """Enable Codex hook feature flags, falling back to editing config.toml.

    Returns:
        tuple[bool, str]: ``(success, message)``. The message describes
        either the successful path taken (CLI vs. direct config write)
        or the failure mode.
    """
    messages: list[str] = []
    for feature in ("hooks", "plugin_hooks"):
        result = _run_codex(["features", "enable", feature])
        if result.returncode == 0:
            messages.append(f"codex features enable {feature}")
            continue
        if _set_toml_feature(_CODEX_CONFIG_PATH, feature, True):
            messages.append(f"set {feature} = true in {_CODEX_CONFIG_PATH}")
            continue
        detail = (
            result.stderr or result.stdout or "could not update Codex config"
        ).strip()
        prior = f" (succeeded earlier: {'; '.join(messages)})" if messages else ""
        return False, f"{feature}: {detail}{prior}"
    return True, "; ".join(messages)


def _register_codex_marketplace(root: Path) -> tuple[bool, str]:
    """Register ``root`` as a Codex marketplace, replacing a stale entry if needed.

    On success runs ``plugin marketplace upgrade reflexioai`` to refresh
    Codex's cache. On the "different source" error path, removes the
    existing registration and retries once.

    Args:
        root: Local marketplace directory to register.

    Returns:
        tuple[bool, str]: ``(success, message)`` where the message
            captures the path taken or the Codex CLI's error output.
    """
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


def _configure_reflexio_setup() -> bool:
    """Load setup state and ensure local defaults when unmanaged.

    Returns:
        bool: Whether read-only mode is enabled.
    """
    env_config.load_reflexio_env(_REFLEXIO_ENV_PATH)
    try:
        env_text = _REFLEXIO_ENV_PATH.read_text()
    except OSError:
        env_text = ""
    read_only_value = ""
    file_api_key = ""
    file_url = ""
    for line in env_text.splitlines():
        parsed = env_config.parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key == env_config.REFLEXIO_API_KEY_ENV:
            file_api_key = value
        elif key == env_config.REFLEXIO_URL_ENV:
            file_url = value
        elif key == "REFLEXIO_USER_ID":
            os.environ[key] = value
        elif key == env_config.CLAUDE_SMART_READ_ONLY_ENV:
            read_only_value = value
    api_key = (
        file_api_key or os.environ.get(env_config.REFLEXIO_API_KEY_ENV, "")
    ).strip()
    read_only = read_only_value.strip().lower() in {"1", "true", "yes", "on"}
    if api_key:
        reflexio_url = (
            file_url
            or os.environ.get(env_config.REFLEXIO_URL_ENV, _MANAGED_REFLEXIO_URL)
        ).strip()
        os.environ[env_config.REFLEXIO_URL_ENV] = reflexio_url
        os.environ[env_config.REFLEXIO_API_KEY_ENV] = api_key
        os.environ["CLAUDE_SMART_MANAGED_SETUP"] = "1"
        sys.stdout.write(
            f"Using managed Reflexio at {reflexio_url} "
            f"(API key {env_config.mask_secret(api_key)}).\n"
        )
    else:
        os.environ.pop(env_config.REFLEXIO_URL_ENV, None)
        os.environ.pop(env_config.REFLEXIO_API_KEY_ENV, None)
        os.environ.pop("REFLEXIO_USER_ID", None)
        os.environ.pop("CLAUDE_SMART_MANAGED_SETUP", None)
        added = env_config.ensure_local_env_defaults(_REFLEXIO_ENV_PATH)
        if added:
            sys.stdout.write(f"Seeded {_REFLEXIO_ENV_PATH} with {', '.join(added)}.\n")
    return read_only


def cmd_install_codex(args: argparse.Namespace) -> int:
    """Install the claude-smart plugin marketplace for Codex.

    Only supported from a source checkout — the wheel ships the Python
    package without the repo-level ``.agents/`` and top-level ``plugin/``
    layout this command expects. End users install via the npm wrapper.

    Args:
        _args: Parsed CLI args (unused).

    Returns:
        int: 0 on success, non-zero on failure or unsupported runtime.
    """
    if not shutil.which("codex"):
        sys.stderr.write("error: 'codex' CLI not found on PATH. Install Codex first.\n")
        return 1
    if not shutil.which("uv"):
        sys.stderr.write(
            "error: 'uv' not found on PATH. Install uv or restart your shell.\n"
        )
        return 1
    read_only = _configure_reflexio_setup()

    missing = _missing_codex_marketplace_files(_REPO_ROOT)
    if missing:
        sys.stderr.write(
            "error: `claude-smart install --host codex` (Python path) only runs "
            "from a source checkout. End users: `npx claude-smart install --host "
            "codex` instead.\n"
        )
        return 1
    marketplace_root = _prepare_codex_local_marketplace()
    if read_only:
        _prune_publish_hooks_for_read_only(marketplace_root / _CODEX_LOCAL_PLUGIN_PATH)

    hooks_ok, hooks_msg = _enable_codex_plugin_hooks()
    if hooks_ok:
        sys.stdout.write(f"Enabled Codex plugin hooks ({hooks_msg}).\n")
    else:
        sys.stderr.write(f"warning: could not enable Codex plugin hooks: {hooks_msg}\n")

    registered, registration_msg = _register_codex_marketplace(marketplace_root)
    if registered:
        sys.stdout.write(f"{registration_msg}.\n")
    else:
        sys.stderr.write(f"warning: {registration_msg}\n")

    installed = False
    install_msg = "marketplace registration failed"
    trusted = False
    trust_msg = "plugin was not installed"
    if registered:
        installed, install_msg = _install_codex_plugin_cache(
            marketplace_root / _CODEX_LOCAL_PLUGIN_PATH
        )
        if installed:
            sys.stdout.write(f"{install_msg}.\n")
            if read_only:
                sys.stdout.write(
                    "Installed read-only hook manifest; publish interactions hooks are disabled.\n"
                )
            trusted, trust_msg = _trust_codex_plugin_hooks(Path.cwd())
            if not trusted:
                # The app-server can race against the just-installed cache.
                # Retry once before giving up; the manual /hooks recovery is
                # available either way.
                time.sleep(0.5)
                trusted, trust_msg = _trust_codex_plugin_hooks(Path.cwd())
            if trusted:
                sys.stdout.write(f"{trust_msg}.\n")
            else:
                sys.stderr.write(f"warning: {trust_msg}\n")
        else:
            sys.stderr.write(f"warning: {install_msg}\n")

    if registered and installed and trusted:
        sys.stdout.write(
            "\nclaude-smart Codex support is installed.\n"
            "Restart Codex so the installed plugin and trusted hooks reload. /plugins should "
            f"show claude-smart as installed from the {_CODEX_MARKETPLACE_DISPLAY_NAME} marketplace. "
            "Uninstall removes the plugin cache and marketplace registration but leaves "
            "shared claude-smart data and Codex's global hook feature flags intact.\n"
        )
    elif registered and installed:
        sys.stdout.write(
            "\nclaude-smart Codex support is installed, but hook trust could not be completed.\n"
            "Fully quit and reopen Codex in this repo, run /hooks, trust the claude-smart hooks, "
            "and restart Codex so hooks reload.\n"
        )
    elif registered:
        sys.stdout.write(
            "\nclaude-smart Codex marketplace is prepared, but automatic plugin install failed.\n"
            "Fully quit and reopen Codex in this repo, run /plugins, install claude-smart from "
            f"the {_CODEX_MARKETPLACE_DISPLAY_NAME} marketplace, and restart Codex so hooks reload.\n"
        )
    else:
        sys.stdout.write(
            "\nCodex hooks enabled; marketplace registration failed.\n"
            f"Install manually with `codex plugin marketplace add {marketplace_root}`, "
            "then fully quit and reopen Codex, run /plugins, install claude-smart from the "
            f"{_CODEX_MARKETPLACE_DISPLAY_NAME} marketplace, and restart Codex so hooks reload.\n"
        )
    return 0 if hooks_ok and registered and installed and trusted else 1


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
    read_only = _configure_reflexio_setup()

    for cmd in (
        ["claude", "plugin", "marketplace", "add", args.source],
        ["claude", "plugin", "install", _PLUGIN_SPEC],
    ):
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            sys.stderr.write(f"error: {' '.join(cmd)} failed (exit {exc.returncode})\n")
            return exc.returncode or 1

    bootstrapped, message = _bootstrap_claude_code_install()
    if not bootstrapped:
        sys.stderr.write(
            f"error: claude-smart installed, but dependency bootstrap failed: {message}\n"
        )
        sys.stderr.write(
            "Fix the issue above, then run /claude-smart:restart or restart Claude Code to retry.\n"
        )
        return 1
    _restore_publish_hooks_from_source(Path(message))
    if read_only:
        _prune_publish_hooks_for_read_only(Path(message))
        sys.stdout.write(
            "Installed read-only hook manifest; publish interactions hooks are disabled.\n"
        )
    sys.stdout.write(f"Prepared claude-smart runtime at {message}.\n")

    sys.stdout.write(
        "\nclaude-smart installed and dependencies are prepared. "
        "Restart Claude Code in your project.\n"
    )
    return 0


def cmd_update(args: argparse.Namespace) -> int:
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

    read_only = _configure_reflexio_setup()
    cmd = ["claude", "plugin", "update", _PLUGIN_SPEC]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"error: {' '.join(cmd)} failed (exit {exc.returncode})\n")
        return exc.returncode or 1

    sys.stdout.write("\nclaude-smart updated. Restart Claude Code to apply.\n")
    plugin_root = _find_claude_code_plugin_root()
    if plugin_root is not None:
        _restore_publish_hooks_from_source(plugin_root)
        if read_only:
            _prune_publish_hooks_for_read_only(plugin_root)
            sys.stdout.write(
                "Installed read-only hook manifest; publish interactions hooks are disabled.\n"
            )
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
        f"{_LOCAL_DATA_NOTICE}"
    )
    return 0


def cmd_uninstall_codex(_args: argparse.Namespace) -> int:
    """Remove the Codex marketplace registration and local install state.

    Runs ``codex plugin marketplace remove reflexioai`` (skipped when the
    Codex CLI is absent) and then cleans the on-disk marketplace
    wrapper, plugin cache, and the corresponding sections of
    ``~/.codex/config.toml``. Shared learning data is preserved.

    Args:
        _args: Parsed CLI args (unused).

    Returns:
        int: 0 on success or partial cleanup; non-zero is reserved for
            future failure modes — current paths swallow Codex CLI
            errors and continue cleaning local state.
    """
    if not shutil.which("codex"):
        sys.stdout.write("Codex CLI not found; skipping marketplace removal.\n")
        _cleanup_codex_install_state()
        return 0
    result = _run_codex(["plugin", "marketplace", "remove", _CODEX_MARKETPLACE_NAME])
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        sys.stderr.write(
            "warning: Codex marketplace removal failed"
            + (f": {output}" if output else "")
            + "\n"
        )
    if not _cleanup_codex_install_state():
        sys.stderr.write(f"warning: could not update {_CODEX_CONFIG_PATH}\n")
    sys.stdout.write(
        "claude-smart Codex plugin and marketplace state removed. Restart Codex to apply. "
        "Codex's global hook feature flags were left in place.\n"
        f"{_LOCAL_DATA_NOTICE}"
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
    sys.stdout.write(
        md or f"_No skills or preferences yet for project `{project_id}`._\n"
    )
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    """Force reflexio extraction over the active session's interactions.

    Publishes unpublished interactions with ``force_extraction=True`` and
    ``override_learning_stall=True`` as an explicit retry request rather than
    waiting for the next batch interval. The publish call itself remains
    non-blocking: ``Adapter.publish`` sends ``wait_for_response=False``.
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
    bash = _resolve_bash()
    if not bash:
        sys.stderr.write(
            "error: bash is required to run claude-smart service scripts\n"
        )
        return 1
    try:
        subprocess.run([bash, str(script), subcmd], check=True)
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
        result = subprocess.run(
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
                    [npm, "ci", "--no-audit", "--no-fund"]
                    if (_DASHBOARD_DIR / "package-lock.json").exists()
                    else [npm, "install", "--no-audit", "--no-fund"]
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
                subprocess.run([npm, "run", "build"], cwd=_DASHBOARD_DIR, check=True)
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
    env_config.load_reflexio_env()
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
