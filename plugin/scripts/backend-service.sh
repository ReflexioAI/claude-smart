#!/usr/bin/env bash
# Auto-start the reflexio FastAPI backend (port 8071) if it's not already
# running. Mirrors dashboard-service.sh: detached spawn, returns immediately
# so the SessionStart hook doesn't block the session.
#
# Subcommands:
#   start         probe /health; if nothing we recognize is on the port,
#                 spawn `uv run reflexio services start --only backend
#                 --no-reload` detached. Polls /health briefly so first
#                 use after session start lands on a warm server, then
#                 returns a continue payload regardless.
#   stop          SIGTERM the recorded process group, escalating to
#                 SIGKILL after a short grace period.
#   session-end   no-op by default; only stops the backend if
#                 CLAUDE_SMART_BACKEND_STOP_ON_END=1 (opt-in — the
#                 backend is intended to be long-lived across sessions).
#   status        print "running on http://localhost:PORT" or "not running".
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
claude_smart_source_login_path
claude_smart_prepend_astral_bins

CMD="${1:-start}"
PORT=8071
# Pass through to `reflexio services start/stop` so the spawned backend
# binds to PORT instead of reflexio's library default (8081).
export BACKEND_PORT="$PORT"

# Default: route extraction through the active host CLI + ONNX embedder
# so claude-smart works without any LLM API key. Users can opt out by
# pre-exporting these to 0.
export CLAUDE_SMART_USE_LOCAL_CLI="${CLAUDE_SMART_USE_LOCAL_CLI:-1}"
export CLAUDE_SMART_USE_LOCAL_EMBEDDING="${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-1}"
# The backend can be spawned from contexts whose PATH lacks the host
# CLI dir (commonly ~/.local/bin or /opt/homebrew/bin). Pin the CLI
# explicitly if we can resolve it from our own (post-login-path) PATH.
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"

if [ -z "${CLAUDE_SMART_CLI_PATH:-}" ]; then
  if [ "${CLAUDE_SMART_HOST:-claude-code}" = "codex" ]; then
    # Reflexio's provider still calls CLAUDE_SMART_CLI_PATH with Claude CLI
    # flags. Use a small compatibility executable that translates that narrow
    # contract to `codex exec`.
    claude_smart_prepend_node_bins
    export CLAUDE_SMART_CLI_PATH="$PLUGIN_ROOT/scripts/codex-claude-compat"
  elif _cs_cli_path=$(command -v claude 2>/dev/null) && [ -n "$_cs_cli_path" ]; then
    export CLAUDE_SMART_CLI_PATH="$_cs_cli_path"
  elif [ -x "$HOME/.local/bin/claude" ]; then
    export CLAUDE_SMART_CLI_PATH="$HOME/.local/bin/claude"
  fi
  unset _cs_cli_path
fi

STATE_DIR="$HOME/.claude-smart"
PID_FILE="$STATE_DIR/backend.pid"
LOG_FILE="$STATE_DIR/backend.log"
LOG_MAX_BYTES="$(claude_smart_log_max_bytes)"
mkdir -p "$STATE_DIR"
claude_smart_trim_log_file "$LOG_FILE" "$LOG_MAX_BYTES"

emit_ok() { echo '{"continue":true,"suppressOutput":true}'; }

emit_start_failure() {
  reason="$1"
  if py=$(claude_smart_resolve_python 2>/dev/null); then
    "$py" - "$reason" <<'PY'
import json
import sys

reason = sys.argv[1].strip()
message = (
    "> **claude-smart learning backend is not running.** "
    "Interactions are being buffered locally, but learning will not publish "
    "until the backend starts.\n"
)
if reason:
    message += f">\n> Last startup error: `{reason}`\n"
message += (
    ">\n> Make sure the local model provider is available: Claude Code needs "
    "`claude`, Codex needs `codex`. Then run `/claude-smart:restart`."
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": message,
    }
}))
PY
  else
    emit_ok
  fi
}

# Tree-kill the recorded process. Delegates to claude_smart_kill_tree
# (POSIX: signal the process group; Windows: taskkill /T /F /PID).
kill_group() {
  claude_smart_kill_tree "$1"
}

# True if /health returns 200. Reflexio's /health is a plain GET with no
# marker header, so we can't distinguish our backend from someone else's
# reflexio on the same port — if you run two reflexio instances on 8071
# you'll get collision regardless of what we do here.
backend_healthy() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null
}

# True only if the recorded PID is alive AND /health responds. A stale
# PID file from a crashed backend is not enough — we must see the port
# actually answer, so next hook retries cleanly.
is_our_backend_running() {
  if [ -f "$PID_FILE" ]; then
    pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      backend_healthy && return 0
    fi
  fi
  # Recover from a missing PID file if a foreign-but-functional reflexio
  # is already serving — no need to start a second one.
  backend_healthy && return 0
  return 1
}

# True if *anything* is listening on the port (even non-HTTP). Used to
# avoid stomping on a foreign listener with a failed-to-start uvicorn.
port_occupied() {
  (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

# Reap any reflexio/uvicorn listener still holding $PORT after the PID
# file kill. Filters by cmdline so we don't knock over an unrelated
# service a user has bound to 8071 — symmetric with start's refusal to
# stomp on a foreign listener. Silent on failure.
reap_port_listeners() {
  command -v lsof >/dev/null 2>&1 || return 0
  candidates=$(lsof -ti:"$PORT" 2>/dev/null) || candidates=""
  [ -z "$candidates" ] && return 0
  ours=""
  for pid in $candidates; do
    cmdline=$(ps -p "$pid" -o command= 2>/dev/null || true)
    case "$cmdline" in
      *reflexio*|*uvicorn*) ours="$ours $pid" ;;
    esac
  done
  [ -z "$ours" ] && return 0
  # shellcheck disable=SC2086
  kill -TERM $ours 2>/dev/null || true
  sleep 1
  remaining=""
  for pid in $ours; do
    kill -0 "$pid" 2>/dev/null && remaining="$remaining $pid"
  done
  [ -z "$remaining" ] && return 0
  # shellcheck disable=SC2086
  kill -KILL $remaining 2>/dev/null || true
}

# Full shutdown: kill the recorded process group (if any) then sweep the
# port for surviving reflexio listeners. Used by both `stop` and the
# opt-in `session-end` path so a stale/missing PID file doesn't produce
# a silent no-op.
full_stop() {
  if [ -f "$PID_FILE" ]; then
    kill_group "$(cat "$PID_FILE" 2>/dev/null)"
    rm -f "$PID_FILE"
  fi
  reap_port_listeners
}

case "$CMD" in
  start)
    # Opt-out: users who don't want the backend managed by the hook can
    # set CLAUDE_SMART_BACKEND_AUTOSTART=0.
    if [ "${CLAUDE_SMART_BACKEND_AUTOSTART:-1}" = "0" ]; then
      emit_ok; exit 0
    fi
    if is_our_backend_running; then emit_ok; exit 0; fi
    if port_occupied; then
      # Something answered the TCP probe but /health didn't — don't
      # start a second uvicorn on top of it.
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "[claude-smart] backend: port $PORT held by another process; skipping"
      emit_ok; exit 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "[claude-smart] backend: uv not on PATH; skipping"
      emit_ok; exit 0
    fi
    cd "$PLUGIN_ROOT"

    # Cap local interaction history to keep the SQLite store small for
    # claude-smart users. Reflexio's library defaults are much higher
    # (250k/50k) for server deployments; here we override only in the
    # claude-smart plugin context. Users can still override via env.
    export INTERACTION_CLEANUP_THRESHOLD="${INTERACTION_CLEANUP_THRESHOLD:-500}"
    export INTERACTION_CLEANUP_DELETE_COUNT="${INTERACTION_CLEANUP_DELETE_COUNT:-200}"

    # --no-reload: uvicorn's reloader forks a supervisor; makes
    # bookkeeping harder and we don't need hot-reload for a user-facing
    # service. Detach via claude_smart_spawn_detached so the same code
    # path covers Linux (setsid), macOS (python3 os.setsid), and Windows
    # (nohup; no process groups). backend-log-runner.sh owns stdout/stderr
    # capture so process output cannot grow backend.log past its cap.
    claude_smart_spawn_detached bash "$HERE/backend-log-runner.sh" \
      "$LOG_FILE" "$LOG_MAX_BYTES" -- \
      uv run --project "$PLUGIN_ROOT" --quiet \
      reflexio services start --only backend --no-reload
    svc_pid=$!
    # Record the spawned pid, not a pgid sampled with ps. On POSIX,
    # setsid/python os.setsid make this pid the new process group leader;
    # sampling immediately can race and capture the caller's pgid instead.
    # On Windows, claude_smart_kill_tree translates the MSYS pid to WINPID.
    echo "$svc_pid" > "$PID_FILE"

    # Give uvicorn up to ~10s to answer /health. The very first boot
    # after a fresh checkout may be slower (LiteLLM import, chromadb
    # warmup) — dashboard auto-start does the same thing. We always
    # return ok; the backend catches up in background if it needs to.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      backend_healthy && break
      sleep 1
    done
    if ! backend_healthy; then
      pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
      if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
        reason=$(tail -n 120 "$LOG_FILE" 2>/dev/null | grep -E "No LLM provider available|No generation-capable LLM provider available|CLI not found|skipping provider registration|Application startup failed" | tail -n 1 | sed 's/^[[:space:]]*//')
        emit_start_failure "$reason"
        exit 0
      fi
    fi
    emit_ok
    ;;
  stop)
    full_stop
    emit_ok
    ;;
  session-end)
    # Default: leave the backend running so learning keeps flowing
    # between sessions. Opt in to teardown with
    # CLAUDE_SMART_BACKEND_STOP_ON_END=1.
    if [ "${CLAUDE_SMART_BACKEND_STOP_ON_END:-0}" = "1" ]; then
      full_stop
    fi
    emit_ok
    ;;
  status)
    if is_our_backend_running; then echo "running on http://localhost:$PORT"; else echo "not running"; fi
    ;;
  *)
    emit_ok
    ;;
esac
