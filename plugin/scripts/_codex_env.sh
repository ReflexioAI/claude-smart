# Shared bootstrap for Codex hook commands. This file is sourced by hook
# entries after a tiny root-discovery step, so keep it POSIX-shell compatible.

_HP=$(printenv PATH 2>/dev/null || true)
if [ -z "$_HP" ] && [ -n "${SHELL:-}" ]; then
  _HP=$("$SHELL" -lc 'printf %s "$PATH"' 2>/dev/null || true)
fi
_HP=$(printf '%s' "$_HP" | tr ' ' ':')
export PATH="${_HP:+$_HP:}$PATH"

if [ -z "${_R:-}" ]; then
  _R="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-}}"
fi
if [ -z "$_R" ]; then
  _R=$(ls -dt "$HOME/plugins/claude-smart" \
    "$HOME/.codex/plugins/cache/reflexioai/claude-smart"/*/ \
    2>/dev/null | head -n 1)
fi
if [ -z "$_R" ]; then
  echo "claude-smart: plugin root not found" >&2
  return 1 2>/dev/null || exit 1
fi

_R="${_R%/}"
export _R
export PLUGIN_ROOT="$_R"
export CLAUDE_PLUGIN_ROOT="$_R"
export CLAUDE_SMART_HOST="codex"
