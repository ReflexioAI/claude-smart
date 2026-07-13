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

try_prepared_mcp_root() {
  local candidate candidate_real candidate_python
  candidate="$1"
  shift || true
  [ -n "$candidate" ] || return 1
  [ -f "$candidate/scripts/mcp-server.sh" ] || return 1
  candidate_real="$(claude_smart_canonical_dir "$candidate" 2>/dev/null || true)"
  [ -n "$candidate_real" ] || return 1
  [ "$candidate_real" != "$current_real" ] || return 1
  candidate_python="$(claude_smart_plugin_python "$candidate_real")"
  [ -x "$candidate_python" ] || return 1
  echo "[claude-smart] redirecting MCP server from unprepared plugin root $PLUGIN_ROOT to prepared root $candidate_real" >&2
  exec "${CLAUDE_SMART_BASH:-bash}" "$candidate_real/scripts/mcp-server.sh" "$@"
}

PLUGIN_PYTHON="$(claude_smart_plugin_python "$PLUGIN_ROOT")"
if [ ! -x "$PLUGIN_PYTHON" ]; then
  current_real="$(claude_smart_canonical_dir "$PLUGIN_ROOT" 2>/dev/null || true)"
  try_prepared_mcp_root "$HOME/.reflexio/plugin-root" "$@" || true
  if [ -f "$HOME/.reflexio/plugin-root.txt" ]; then
    try_prepared_mcp_root "$(cat "$HOME/.reflexio/plugin-root.txt" 2>/dev/null || true)" "$@" || true
  fi
  for candidate in $(ls -dt "$HOME/.claude/plugins/cache/reflexioai/claude-smart"/* "$HOME/.codex/plugins/cache/reflexioai/claude-smart"/* 2>/dev/null || true); do
    try_prepared_mcp_root "$candidate" "$@" || true
  done
fi
if [ ! -x "$PLUGIN_PYTHON" ]; then
  echo "[claude-smart] MCP server cannot start: no prepared plugin venv at $PLUGIN_PYTHON and no prepared cache fallback; run \`npx claude-smart install\` to rebuild it" >&2
  exit 1
fi

exec "$PLUGIN_PYTHON" -m claude_smart.mcp_server
