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

# Return 0 (true) if running under a Windows-flavoured bash (Git Bash,
# MSYS, Cygwin). Used to gate POSIX-only primitives (setsid, process
# groups) and route around Windows-specific potholes (the python3 App
# Execution Alias stub at WindowsApps\python3.exe).
claude_smart_is_windows() {
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

# Print the absolute path of a working python interpreter, or nothing
# (and return non-zero) if none is usable. On Windows, `python3` is
# usually the Microsoft Store "App Execution Alias" stub at
# %LocalAppData%\Microsoft\WindowsApps\python3.exe — `command -v python3`
# returns truthy but invoking it just prints a "Python was not found"
# message and exits non-zero. We probe with `-V` to filter the stub out
# and prefer `python` (the real interpreter when one is installed).
claude_smart_resolve_python() {
  if claude_smart_is_windows; then
    for cand in python python3; do
      if command -v "$cand" >/dev/null 2>&1 && "$cand" -V >/dev/null 2>&1; then
        command -v "$cand"
        return 0
      fi
    done
    return 1
  fi
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -V >/dev/null 2>&1; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}

# Spawn a command fully detached from the current shell so a hook timeout
# (Claude Code's install/SessionStart budget) cannot kill it mid-flight.
# POSIX: setsid → python3 os.setsid → nohup (in that order of strength).
# Windows: nohup alone — Git Bash has no setsid, no process groups, and
# `os.setsid()` is POSIX-only; nohup ignores SIGHUP which is enough to
# survive the parent console closing. The python3 fallback is gated on a
# real-interpreter probe (-V) so the Windows App Execution Alias stub
# doesn't get invoked. Caller is responsible for redirecting stdout/stderr;
# we do not impose a log destination here. Stdin is closed so the child
# cannot inherit a tty. Use `$!` after this call to capture the pid.
claude_smart_spawn_detached() {
  if claude_smart_is_windows; then
    nohup "$@" < /dev/null &
    return 0
  fi
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$@" < /dev/null &
  elif _CS_PY=$(claude_smart_resolve_python) && [ -n "$_CS_PY" ]; then
    "$_CS_PY" -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      "$@" < /dev/null &
  else
    nohup "$@" < /dev/null &
  fi
}

# Terminate a process and (on POSIX) its whole process group, escalating
# from TERM to KILL after a short grace period. On Windows there are no
# POSIX process groups, so we use `taskkill /T /F /PID` which walks the
# child-process tree via the Windows job-object/parent-pid relationships
# — the closest equivalent to a group kill.
#
# Windows-specific subtlety: in Git Bash / MSYS, `$!` for a backgrounded
# job returns the MSYS pid (an internal counter), NOT the native Windows
# pid that taskkill needs. `ps -W` (or `-o winpid=`) exposes the WINPID
# column for the translation. If the lookup fails we fall back to
# treating the input as a native pid, so callers can pass either an MSYS
# pid (recorded via $!) or a Windows pid (from tasklist) interchangeably.
# The `//T //F //PID` syntax escapes Git Bash's MSYS path-mangling of
# arguments that begin with `/`.
claude_smart_kill_tree() {
  pid="$1"
  [ -z "$pid" ] && return 0
  if claude_smart_is_windows; then
    # Git Bash's `ps` is the procps fork, not BSD/Linux ps; it has no
    # -o option but its default header is `PID PPID PGID WINPID TTY ...`,
    # so column 4 of the data row is the Windows pid. awk extracts it
    # without depending on -o support.
    target=""
    if command -v ps >/dev/null 2>&1; then
      target=$(ps -p "$pid" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d ' \r\n' || true)
    fi
    [ -z "$target" ] && target="$pid"
    if command -v taskkill >/dev/null 2>&1; then
      taskkill //T //F //PID "$target" >/dev/null 2>&1 || true
    else
      kill -TERM "$pid" 2>/dev/null || true
      sleep 0.5
      kill -KILL "$pid" 2>/dev/null || true
    fi
    return 0
  fi
  current_pgid=""
  if command -v ps >/dev/null 2>&1; then
    current_pgid=$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ')
  fi
  if [ -n "$current_pgid" ] && [ "$pid" = "$current_pgid" ]; then
    return 0
  fi
  if ! kill -TERM -- "-$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.5
    kill -KILL "$pid" 2>/dev/null || true
    return 0
  fi
  for _ in 1 2 3 4 5; do
    kill -0 -- "-$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
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
