#!/usr/bin/env bash
# Wrapper for slash commands that invoke the claude_smart CLI via uv.
# Claude Code runs `!` bash directives in slash command .md files in a
# non-interactive, non-login shell that does NOT source ~/.zshrc or
# ~/.bash_profile. As a result, binaries installed by smart-install.sh
# at ~/.local/bin (e.g. uv from the astral.sh installer) are invisible
# to those directives until the user manually re-sources their shell rc.
# This wrapper bootstraps PATH the same way hook_entry.sh does so the
# slash commands work on a fresh install.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
claude_smart_source_login_path
claude_smart_prepend_astral_bins

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"

# If the Setup hook recorded an install failure, surface that reason
# instead of falling through to a generic "uv not found" — mirrors the
# branch at hook_entry.sh so slash commands and hooks behave consistently
# on a broken install.
FAILURE_MARKER="$HOME/.claude-smart/install-failed"
if [ -f "$FAILURE_MARKER" ]; then
  msg="$(cat "$FAILURE_MARKER" 2>/dev/null || echo "")"
  [ -n "$msg" ] || msg="unknown error"
  echo "claude-smart is not installed correctly: $msg" >&2
  echo "Re-run the plugin's Setup (restart Claude Code) or fix the underlying issue and delete $FAILURE_MARKER to retry." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "claude-smart: 'uv' not found on PATH." >&2
  echo "Install it from https://docs.astral.sh/uv/ or restart Claude Code so the Setup hook can install it." >&2
  exit 1
fi

exec uv run --project "$PLUGIN_ROOT" --quiet python -m claude_smart.cli "$@"
