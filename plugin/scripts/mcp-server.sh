#!/usr/bin/env bash
# Start the claude-smart MCP server. Stdout is reserved for MCP frames.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"

claude_smart_source_login_path
claude_smart_prepend_astral_bins
claude_smart_source_reflexio_env

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
claude_smart_reexec_stable_plugin_root_if_needed "$PLUGIN_ROOT" "mcp-server.sh" "$@"

PLUGIN_PYTHON="$(claude_smart_plugin_python "$PLUGIN_ROOT")"
if [ ! -x "$PLUGIN_PYTHON" ]; then
  current_real="$(claude_smart_canonical_dir "$PLUGIN_ROOT" 2>/dev/null || true)"
  for candidate in "$HOME/.reflexio/plugin-root" $(ls -dt "$HOME/.claude/plugins/cache/reflexioai/claude-smart"/* "$HOME/.codex/plugins/cache/reflexioai/claude-smart"/* 2>/dev/null || true); do
    [ -f "$candidate/scripts/mcp-server.sh" ] || continue
    candidate_real="$(claude_smart_canonical_dir "$candidate" 2>/dev/null || true)"
    [ -n "$candidate_real" ] || continue
    [ "$candidate_real" != "$current_real" ] || continue
    candidate_python="$(claude_smart_plugin_python "$candidate_real")"
    [ -x "$candidate_python" ] || continue
    echo "[claude-smart] redirecting MCP server from unprepared plugin root $PLUGIN_ROOT to prepared root $candidate_real" >&2
    exec bash "$candidate_real/scripts/mcp-server.sh" "$@"
  done
fi
if [ ! -x "$PLUGIN_PYTHON" ]; then
  echo "[claude-smart] MCP server cannot start: no prepared plugin venv at $PLUGIN_PYTHON and no prepared cache fallback; run \`npx claude-smart install\` to rebuild it" >&2
  exit 1
fi

exec "$PLUGIN_PYTHON" -m claude_smart.mcp_server
