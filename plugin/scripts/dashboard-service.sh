#!/usr/bin/env bash
# Auto-start the claude-smart Next.js dashboard (port 3001) if it's not
# already running. Mirrors how claude-mem boots its worker on SessionStart:
# detached, returns immediately so the hook doesn't block the session.
#
# Subcommands:
#   start         probe the port; spawn `npm run start` if our dashboard
#                 isn't already answering. Never builds in foreground — if
#                 .next is missing, logs and bails (Setup is responsible for
#                 the build; rerun it or restart Claude Code to retry).
#   stop          kill the recorded process group, and (if our dashboard
#                 is still responding on the port) kill the port listener
#                 as a fallback — covers dashboards started outside this
#                 script or whose PGID signalling missed
#   session-end   no-op by default; stops the dashboard if
#                 CLAUDE_SMART_DASHBOARD_STOP_ON_END=1 (opt-in — the dashboard
#                 is intended to be long-lived across sessions)
#   status        print "running on http://localhost:PORT" or "not running"
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
claude_smart_source_login_path

CMD="${1:-start}"
PORT=3001

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
DASHBOARD_DIR="$PLUGIN_ROOT/dashboard"

STATE_DIR="$HOME/.claude-smart"
PID_FILE="$STATE_DIR/dashboard.pid"
LOG_FILE="$STATE_DIR/dashboard.log"
mkdir -p "$STATE_DIR"

emit_ok() { echo '{"continue":true,"suppressOutput":true}'; }

# Kill a process group started via setsid. Sends SIGTERM, waits briefly,
# then SIGKILL. Silent on failure — the PID file may point at a process
# that already exited.
kill_group() {
  pgid="$1"
  [ -z "$pgid" ] && return 0
  kill -TERM -- "-$pgid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 -- "-$pgid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -KILL -- "-$pgid" 2>/dev/null || true
}

# True if the marker header served by app/api/health is present on the
# port. Requires curl — absence is reported as false.
marker_responds() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -sfI "http://127.0.0.1:$PORT/api/health" 2>/dev/null \
    | grep -qi '^x-claude-smart-dashboard:'
}

# True only if *our* dashboard is on the port. Uses the marker header so a
# foreign listener on 3001 doesn't cause us to silently skip starting.
is_our_dashboard_running() {
  if [ -f "$PID_FILE" ]; then
    pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      # PID alive — still verify the port responds with our marker so we
      # don't claim "running" when the server crashed but the group leader
      # lingered.
      if command -v curl >/dev/null 2>&1; then
        marker_responds && return 0
      else
        # No curl — fall back to PID liveness alone.
        return 0
      fi
    fi
  fi
  # No PID or dead PID — probe the port for our marker (recovers after a
  # stale PID file from a crash).
  marker_responds && return 0
  return 1
}

# True if *something* is listening on the port, regardless of marker.
port_occupied() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf -o /dev/null "http://127.0.0.1:$PORT" 2>/dev/null && return 0
    # curl with -sfI against a 404/405 still indicates "something answered".
    # Use a connect-only probe as a secondary signal.
  fi
  (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

case "$CMD" in
  start)
    # Opt-out: users who don't want the dashboard long-lived can set
    # CLAUDE_SMART_DASHBOARD_AUTOSTART=0 in their environment.
    if [ "${CLAUDE_SMART_DASHBOARD_AUTOSTART:-1}" = "0" ]; then
      emit_ok; exit 0
    fi
    if [ ! -d "$DASHBOARD_DIR" ]; then emit_ok; exit 0; fi
    if is_our_dashboard_running; then emit_ok; exit 0; fi
    if port_occupied; then
      echo "[claude-smart] dashboard: port $PORT held by another process; skipping" >>"$LOG_FILE"
      emit_ok; exit 0
    fi
    if ! command -v npm >/dev/null 2>&1; then
      echo "[claude-smart] dashboard: npm not on PATH; skipping" >>"$LOG_FILE"
      emit_ok; exit 0
    fi

    # `npm run start` requires a prior `next build`. Do NOT build in the
    # foreground here — SessionStart hooks have a tight timeout and a cold
    # Next build easily exceeds it. If .next is missing, spawn a detached
    # build (dashboard-build.sh) so the first-install cost is paid out of
    # band. dashboard-open.sh detects the build-pid file to surface a
    # "still building" message instead of a generic error.
    if [ ! -d "$DASHBOARD_DIR/.next" ]; then
      BUILD_PID_FILE="$STATE_DIR/dashboard-build.pid"
      if ! claude_smart_pid_alive_file "$BUILD_PID_FILE"; then
        echo "[claude-smart] dashboard: .next missing — starting background build (~1-2 min)" >>"$LOG_FILE"
        claude_smart_spawn_detached bash "$HERE/dashboard-build.sh" >>"$LOG_FILE" 2>&1
      fi
      emit_ok; exit 0
    fi

    cd "$DASHBOARD_DIR"

    # Detach so the hook returns immediately, and put the child in its own
    # session so kill_group can signal the whole tree via a negative PID.
    #   - Linux: setsid is standard.
    #   - macOS: setsid is not installed; use python3 (ships with the OS)
    #     to call os.setsid() before execing npm, which makes the child
    #     session/group leader with PID==PGID.
    #   - Fallback: bare nohup, then derive the real PGID via ps -o pgid.
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup npm run start >>"$LOG_FILE" 2>&1 < /dev/null &
      echo $! > "$PID_FILE"
    elif command -v python3 >/dev/null 2>&1; then
      python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
        npm run start >>"$LOG_FILE" 2>&1 < /dev/null &
      echo $! > "$PID_FILE"
    else
      nohup npm run start >>"$LOG_FILE" 2>&1 < /dev/null &
      dash_pid=$!
      actual_pgid=""
      if command -v ps >/dev/null 2>&1; then
        actual_pgid=$(ps -o pgid= -p "$dash_pid" 2>/dev/null | tr -d ' ')
      fi
      echo "${actual_pgid:-$dash_pid}" > "$PID_FILE"
    fi
    emit_ok
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      kill_group "$(cat "$PID_FILE" 2>/dev/null)"
      rm -f "$PID_FILE"
    fi
    # Fallback: if our dashboard is still responding on the port (e.g.,
    # was started outside this script, or the PGID kill missed because
    # the process wasn't the group leader) kill whoever owns the port.
    # Gated on the marker header so we never touch a foreign listener.
    if marker_responds && command -v lsof >/dev/null 2>&1; then
      port_pid=$(lsof -t -i ":$PORT" -sTCP:LISTEN 2>/dev/null | head -n1)
      if [ -n "$port_pid" ]; then
        kill -TERM "$port_pid" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
          kill -0 "$port_pid" 2>/dev/null || break
          sleep 0.2
        done
        kill -KILL "$port_pid" 2>/dev/null || true
      fi
    fi
    emit_ok
    ;;
  session-end)
    # Default: leave the dashboard running so users can keep browsing
    # interactions/playbooks between sessions. Opt in to teardown by setting
    # CLAUDE_SMART_DASHBOARD_STOP_ON_END=1 in the environment.
    if [ "${CLAUDE_SMART_DASHBOARD_STOP_ON_END:-0}" = "1" ]; then
      if [ -f "$PID_FILE" ]; then
        kill_group "$(cat "$PID_FILE" 2>/dev/null)"
        rm -f "$PID_FILE"
      fi
    fi
    emit_ok
    ;;
  status)
    if is_our_dashboard_running; then echo "running on http://localhost:$PORT"; else echo "not running"; fi
    ;;
  *)
    emit_ok
    ;;
esac
