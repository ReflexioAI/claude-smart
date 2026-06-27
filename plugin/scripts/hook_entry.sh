#!/usr/bin/env bash
# Dispatch a Claude Code or Codex hook event to the claude_smart Python package.
# CLAUDE_PLUGIN_ROOT points at the plugin dir (dev: <repo>/plugin;
# installed: ~/.claude/plugins/cache/reflexioai/claude-smart/<version>),
# which is also the Python project root with pyproject.toml + uv.lock.
# We invoke the prepared venv Python directly so hooks use the dependency set
# produced by smart-install.sh, including the vendored Reflexio snapshot it
# installs non-editably (--reinstall --no-deps).
#
# If the Setup hook recorded an install failure at
# ~/.claude-smart/install-failed, short-circuit with a user-visible
# message instead of trying to run uv and failing silently.
set -eu

HOST="claude-code"
EVENT="${1:-}"
case "$EVENT" in
  claude-code|codex|opencode)
    HOST="$EVENT"
    EVENT="${2:-}"
    ;;
esac
export CLAUDE_SMART_HOST="$HOST"
if [ "$HOST" = "codex" ] && [ -z "${CLAUDE_SMART_CITATION_LINK_STYLE:-}" ]; then
  export CLAUDE_SMART_CITATION_LINK_STYLE="markdown"
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
if [ -z "$EVENT" ]; then
  claude_smart_emit_continue
  exit 0
fi

if claude_smart_is_internal_invocation_env; then
  claude_smart_emit_continue
  exit 0
fi
# Pick up uv from the user's login-shell PATH (covers ~/.local/bin populated
# by the astral.sh installer) so a fresh install works before the user
# restarts their terminal. Matches the pattern used by smart-install.sh.
claude_smart_source_login_path
# Explicit fallback for the astral.sh installer's default paths, in case
# the user's login-shell rc hasn't yet been re-sourced to pick them up.
claude_smart_prepend_astral_bins
claude_smart_source_reflexio_env

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
claude_smart_reexec_stable_plugin_root_if_needed "$PLUGIN_ROOT" "hook_entry.sh" "$@"

FAILURE_MARKER="$HOME/.claude-smart/install-failed"
STATE_DIR="$HOME/.claude-smart"
if [ -f "$FAILURE_MARKER" ]; then
  # Auto-clear the marker when install inputs have changed since the
  # failure was recorded (pyproject.toml / uv.lock / smart-install.sh /
  # python interpreter etc.). Without this the plugin stays muted forever
  # after a transient sync failure even when the user has fixed it.
  stored_fp="$(awk -F= '$1=="fingerprint"{print $2; exit}' "$FAILURE_MARKER" 2>/dev/null || true)"
  current_fp="$(claude_smart_install_fingerprint_hash "$PLUGIN_ROOT" "$HERE" 2>/dev/null || true)"
  if [ -z "$stored_fp" ] || [ "$stored_fp" != "$current_fp" ]; then
    rm -f "$FAILURE_MARKER"
  else
    failure_py="$(claude_smart_resolve_python 2>/dev/null || true)"
    if [ "$EVENT" = "session-start" ] && [ -n "$failure_py" ]; then
      "$failure_py" - "$FAILURE_MARKER" <<'PY'
import json, pathlib, sys
first = pathlib.Path(sys.argv[1]).read_text().splitlines()
msg = (first[0].strip() if first else "") or "unknown error"
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": (
            f"> **claude-smart is not installed correctly:** {msg}\n"
            "> Re-run the plugin's Setup (restart your coding assistant) "
            "or fix the underlying issue and delete "
            "`~/.claude-smart/install-failed` to retry."
        ),
    }
}))
PY
    else
      claude_smart_emit_continue
    fi
    exit 0
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  # Self-heal from skipped setup without blocking the first prompt. The
  # installer and Setup hook own dependency bootstrap; hooks only schedule
  # recovery and return so SessionStart stays lightweight.
  if [ "${CLAUDE_SMART_BOOTSTRAPPING:-}" = "1" ]; then
    claude_smart_emit_continue
    exit 0
  fi
  if [ -x "$PLUGIN_ROOT/scripts/smart-install.sh" ]; then
    mkdir -p "$STATE_DIR"
    CLAUDE_SMART_SPAWN_KEEP_OUTPUT=1 claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1 \
      bash "$PLUGIN_ROOT/scripts/smart-install.sh" \
      >>"$STATE_DIR/install.log" 2>&1 || true
  fi
  if ! command -v uv >/dev/null 2>&1; then
    claude_smart_emit_continue
    exit 0
  fi
fi

ensure_hook_package_importable() {
  if claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.hook; then
    return 0
  fi

  if [ "${CLAUDE_SMART_BOOTSTRAPPING:-}" = "1" ]; then
    return 1
  fi
  if [ ! -x "$PLUGIN_ROOT/scripts/smart-install.sh" ]; then
    return 1
  fi

  mkdir -p "$STATE_DIR"
  {
    printf '%s\n' "[claude-smart] hook: claude_smart.hook is not importable; retrying install in background"
    date 2>/dev/null || true
  } >>"$STATE_DIR/install.log" 2>&1
  CLAUDE_SMART_SPAWN_KEEP_OUTPUT=1 claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1 \
    bash "$PLUGIN_ROOT/scripts/smart-install.sh" \
    >>"$STATE_DIR/install.log" 2>&1 || true

  claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.hook
}

if ! ensure_hook_package_importable; then
  claude_smart_emit_continue
  exit 0
fi

# Stdin is the hook payload JSON — stream it through to the Python CLI.
PLUGIN_PYTHON="$(claude_smart_plugin_python "$PLUGIN_ROOT")"
exec "$PLUGIN_PYTHON" -m claude_smart.hook "$HOST" "$EVENT"
