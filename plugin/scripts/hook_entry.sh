#!/usr/bin/env bash
# Dispatch a Claude Code or Codex hook event to the claude_smart Python package.
# CLAUDE_PLUGIN_ROOT points at the plugin dir (dev: <repo>/plugin;
# installed: ~/.claude/plugins/cache/reflexioai/claude-smart/<version>),
# which is also the Python project root with pyproject.toml + uv.lock.
# We invoke via `uv run --project` so the pinned env from uv.lock is used.
#
# If the Setup hook recorded an install failure at
# ~/.claude-smart/install-failed, short-circuit with a user-visible
# message instead of trying to run uv and failing silently.
set -eu

HOST="claude-code"
EVENT="${1:-}"
case "$EVENT" in
  claude-code|codex)
    HOST="$EVENT"
    EVENT="${2:-}"
    ;;
esac
if [ -z "$EVENT" ]; then
  echo '{"continue":true,"suppressOutput":true}'
  exit 0
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
# Pick up uv from the user's login-shell PATH (covers ~/.local/bin populated
# by the astral.sh installer) so a fresh install works before the user
# restarts their terminal. Matches the pattern used by smart-install.sh.
claude_smart_source_login_path
# Explicit fallback for the astral.sh installer's default paths, in case
# the user's login-shell rc hasn't yet been re-sourced to pick them up.
claude_smart_prepend_astral_bins

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"

FAILURE_MARKER="$HOME/.claude-smart/install-failed"
if [ -f "$FAILURE_MARKER" ]; then
  if [ "$EVENT" = "session-start" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "$FAILURE_MARKER" <<'PY'
import json, pathlib, sys
msg = pathlib.Path(sys.argv[1]).read_text().strip() or "unknown error"
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
    echo '{"continue":true,"suppressOutput":true}'
  fi
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  # uv missing post-install → don't crash the session, just no-op.
  echo '{"continue":true,"suppressOutput":true}'
  exit 0
fi

# Stdin is the hook payload JSON — stream it through to the Python CLI.
exec uv run --project "$PLUGIN_ROOT" --quiet python -m claude_smart.hook "$HOST" "$EVENT"
