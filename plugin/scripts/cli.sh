#!/usr/bin/env bash
# Dispatch slash commands to the selected Python runtime. Standalone
# installations can discover tools and finish a skipped Setup step.
set -eu

# The Python runtime does not own integration installation or removal.
case "${1:-}" in
  install|update|uninstall)
    printf 'claude-smart: use npx claude-smart %s for integration management.\n' "$1" >&2
    exit 2
    ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
claude_smart_reexec_stable_plugin_root_if_needed "$PLUGIN_ROOT" "cli.sh" "$@"
if [ ! -f "$PLUGIN_ROOT/guide-prepared-runtime" ] || [ ! -f "$PLUGIN_ROOT/guide-git" ]; then
  claude_smart_source_login_path
fi
claude_smart_prepend_astral_bins
claude_smart_prepend_node_bins
claude_smart_source_reflexio_env

# A guided generation already owns its interpreter. Repair belongs to setup.
if [ -f "$PLUGIN_ROOT/guide-prepared-runtime" ]; then
  if ! claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.cli; then
    echo "claude-smart: the prepared Python runtime is unavailable. Rerun npx claude-smart setup." >&2
    exit 1
  fi
  PLUGIN_PYTHON="$(claude_smart_plugin_python "$PLUGIN_ROOT")"
  exec "$PLUGIN_PYTHON" -m claude_smart.cli "$@"
fi

# This account-wide diagnostic may come from another host. Only a
# runtime with a broken interpreter should be blocked by it.
FAILURE_MARKER="$HOME/.claude-smart/install-failed"
if [ -f "$FAILURE_MARKER" ] && ! claude_smart_python_imports "$PLUGIN_ROOT" claude_smart.cli; then
  msg="$(head -n 1 "$FAILURE_MARKER" 2>/dev/null || echo "")"
  [ -n "$msg" ] || msg="unknown error"
  echo "claude-smart is not installed correctly: $msg" >&2
  echo "Fix the underlying issue, then rerun npx claude-smart install (add --host codex or --host opencode for those hosts) to retry dependency setup." >&2
  exit 1
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
      echo "Fix the underlying issue, then rerun npx claude-smart install (add --host codex or --host opencode for those hosts) to retry dependency setup." >&2
    else
      echo "claude-smart: claude_smart package is not importable in $PLUGIN_ROOT/.venv." >&2
      echo "Run $PLUGIN_ROOT/scripts/smart-install.sh manually to diagnose." >&2
    fi
    exit 1
  fi
fi

PLUGIN_PYTHON="$(claude_smart_plugin_python "$PLUGIN_ROOT")"
exec "$PLUGIN_PYTHON" -m claude_smart.cli "$@"
