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
claude_smart_prepend_node_bins
claude_smart_source_reflexio_env

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
claude_smart_reexec_stable_plugin_root_if_needed "$PLUGIN_ROOT" "cli.sh" "$@"

# If the Setup hook recorded an install failure, surface that reason
# instead of falling through to a generic "uv not found" — mirrors the
# branch at hook_entry.sh so slash commands and hooks behave consistently
# on a broken install.
FAILURE_MARKER="$HOME/.claude-smart/install-failed"
if [ -f "$FAILURE_MARKER" ]; then
  msg="$(head -n 1 "$FAILURE_MARKER" 2>/dev/null || echo "")"
  [ -n "$msg" ] || msg="unknown error"
  echo "claude-smart is not installed correctly: $msg" >&2
  echo "Re-run the plugin's Setup (restart Claude Code) or fix the underlying issue and delete $FAILURE_MARKER to retry." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  # Self-heal: the Setup/SessionStart hook may have been skipped (trust
  # prompt declined, plugin enabled mid-session, etc.) leaving the install
  # half-done. Run smart-install.sh inline so the user does not have to
  # restart Claude Code just to recover.
  # Guard against recursion: if smart-install.sh ever shells back through
  # this wrapper (e.g. a future migration step) we must not loop forever.
  if [ "${CLAUDE_SMART_BOOTSTRAPPING:-}" = "1" ]; then
    echo "claude-smart: bootstrap recursion detected; aborting." >&2
    exit 1
  fi
  if [ -x "$PLUGIN_ROOT/scripts/smart-install.sh" ]; then
    echo "claude-smart: 'uv' not found — bootstrapping dependencies (~1-3 min on first install)..." >&2
    CLAUDE_SMART_BOOTSTRAPPING=1 bash "$PLUGIN_ROOT/scripts/smart-install.sh" >/dev/null
    claude_smart_prepend_astral_bins
    claude_smart_prepend_node_bins
  fi
  if ! command -v uv >/dev/null 2>&1; then
    if [ -f "$FAILURE_MARKER" ]; then
      msg="$(head -n 1 "$FAILURE_MARKER" 2>/dev/null || echo "")"
      [ -n "$msg" ] || msg="unknown error"
      echo "claude-smart: install failed: $msg" >&2
      echo "Fix the underlying issue and delete $FAILURE_MARKER to retry." >&2
    else
      echo "claude-smart: 'uv' not found on PATH after bootstrap attempt." >&2
      echo "Install it from https://docs.astral.sh/uv/ or rerun $PLUGIN_ROOT/scripts/smart-install.sh manually." >&2
    fi
    exit 1
  fi
fi

# Slash commands run synchronously and surface stderr directly to the user,
# so a bare `python -m claude_smart.cli` ModuleNotFoundError leaks to the
# transcript when the install is mid-bootstrap (e.g. SessionStart's
# background smart-install.sh hasn't finished yet). Mirror hook_entry.sh's
# ensure_hook_package_importable check: run smart-install.sh inline once,
# then re-check, then surface either the install-failed marker reason or a
# clear "still bootstrapping" message instead of letting Python crash.
if ! claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.cli; then
  if [ "${CLAUDE_SMART_BOOTSTRAPPING:-}" != "1" ] \
     && [ -x "$PLUGIN_ROOT/scripts/smart-install.sh" ]; then
    echo "claude-smart: finishing install (~1-3 min on first install)..." >&2
    CLAUDE_SMART_BOOTSTRAPPING=1 bash "$PLUGIN_ROOT/scripts/smart-install.sh" >/dev/null || true
  fi
  if ! claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.cli; then
    if [ -f "$FAILURE_MARKER" ]; then
      msg="$(head -n 1 "$FAILURE_MARKER" 2>/dev/null || echo "")"
      [ -n "$msg" ] || msg="unknown error"
      echo "claude-smart is not installed correctly: $msg" >&2
      echo "Re-run the plugin's Setup (restart Claude Code) or fix the underlying issue and delete $FAILURE_MARKER to retry." >&2
    else
      echo "claude-smart: claude_smart package is not importable in $PLUGIN_ROOT/.venv." >&2
      echo "Run $PLUGIN_ROOT/scripts/smart-install.sh manually to diagnose." >&2
    fi
    exit 1
  fi
fi

exec uv run --project "$PLUGIN_ROOT" --no-sync --quiet python -m claude_smart.cli "$@"
