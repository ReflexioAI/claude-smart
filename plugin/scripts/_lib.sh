# Shared helpers for claude-smart plugin scripts. Source, do not execute.

# Claude Code hooks run with a minimal non-interactive PATH that often omits
# nvm/asdf/brew shims where `npm`, `uv`, etc. live. Pull the user's login-shell
# PATH the same way claude-mem does so hook-spawned scripts find them without
# the user having to mutate their global PATH. Best-effort — failures silent.
claude_smart_source_login_path() {
  if [ -n "${SHELL:-}" ] && [ -x "$SHELL" ]; then
    if _SHELL_PATH="$("$SHELL" -lc 'printf %s "$PATH"' 2>/dev/null)"; then
      [ -n "$_SHELL_PATH" ] && export PATH="$_SHELL_PATH:$PATH"
    fi
  fi
}

# Prepend the astral.sh installer's default bin directories to PATH so a
# freshly-installed `uv` is reachable before the user re-sources their
# shell rc. Prepend (not append) so the just-installed binary wins over
# any stale copy earlier in PATH. Literals only — no subshell, so safe
# under `set -u`.
claude_smart_prepend_astral_bins() {
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
}

# Spawn a command fully detached from the current shell so a hook timeout
# (Claude Code's install/SessionStart budget) cannot kill it mid-flight.
# Picks the strongest available primitive: setsid → python3 os.setsid → nohup.
# Caller is responsible for redirecting stdout/stderr; we do not impose a
# log destination here. Stdin is closed so the child cannot inherit a tty.
claude_smart_spawn_detached() {
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$@" < /dev/null &
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      "$@" < /dev/null &
  else
    nohup "$@" < /dev/null &
  fi
}

# Return 0 (true) if $1 names a pid file whose pid is currently alive.
# Silent on missing/empty/stale files.
claude_smart_pid_alive_file() {
  pid_file="$1"
  [ -f "$pid_file" ] || return 1
  pid=$(cat "$pid_file" 2>/dev/null || echo "")
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}
