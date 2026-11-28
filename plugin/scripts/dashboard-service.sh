#!/usr/bin/env bash
# Auto-start the claude-smart Next.js dashboard if it's not already running.
# Mirrors how claude-mem boots its worker on SessionStart:
# detached, returns immediately so the hook doesn't block the session.
#
# Subcommands:
#   start         probe the port; spawn local `next start` if no claude-smart
#                 dashboard is already answering. Never builds in foreground — if
#                 .next is missing, logs and bails (Setup is responsible for
#                 the build; rerun it or restart Claude Code to retry).
#   stop          kill the recorded process group, and (if our dashboard
#                 is still responding on the port) kill the port listener
#                 as a fallback — covers dashboards started outside this
#                 script or whose PGID signalling missed
#   status        print "running on http://localhost:PORT" or "not running"
# Session completion does not own this shared service's lifetime.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
CMD="${1:-start}"
if [ "$CMD" = "session-end" ]; then
  # Cached manifests can outlive their hooks update. Do not load configuration,
  # probe processes or stop shared services on behalf of an ending session.
  claude_smart_emit_continue
  exit 0
fi
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
claude_smart_reexec_stable_plugin_root_if_needed "$PLUGIN_ROOT" "dashboard-service.sh" "$@"
claude_smart_append_service_paths "$PLUGIN_ROOT"
prepared_node=""
if [ -f "$PLUGIN_ROOT/guide-prepared-runtime" ]; then
  node_root="$HOME/.claude-smart/node/current"
  if claude_smart_is_windows; then
    node_candidate="$node_root/node.exe"
  else
    node_candidate="$node_root/bin/node"
  fi
  if [ -x "$node_candidate" ]; then
    prepared_node="$node_candidate"
  fi
fi
claude_smart_prepend_node_bins
claude_smart_source_reflexio_env

BACKEND_PORT="${BACKEND_PORT:-8071}"
dashboard_port="$(claude_smart_dashboard_port)"
export BACKEND_PORT
claude_smart_derive_reflexio_url_from_backend_port

DASHBOARD_DIR="$PLUGIN_ROOT/dashboard"
STATE_DIR="$HOME/.claude-smart"
PID_FILE="$STATE_DIR/dashboard.pid"
LOG_FILE="$STATE_DIR/dashboard.log"
mkdir -p "$STATE_DIR"

emit_ok() { claude_smart_emit_continue; }

# Tree-kill the recorded process. Delegates to claude_smart_kill_tree
# (POSIX: signal the process group; Windows: taskkill /T /F /PID).
kill_group() {
  claude_smart_kill_tree "$1"
}

dashboard_pid_command_matches() {
  pid="$1"
  cmdline="$(claude_smart_pid_command "$pid" | tr '\n' ' ' || true)"
  [ -n "$cmdline" ] || return 1
  case "$cmdline" in
    *"$DASHBOARD_DIR"*|*next-server*|*next*start*|*node*next*|*claude-smart*dashboard*) return 0 ;;
    *) return 1 ;;
  esac
}

kill_recorded_dashboard() {
  if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && dashboard_pid_command_matches "$pid"; then
      kill_group "$pid"
    fi
    rm -f "$PID_FILE"
  fi
}

# True if the marker header served by app/api/health is present on the
# port. A prepared runtime probes with its own Node. This deliberately
# accepts any claude-smart dashboard, including stale cache versions, so
# stop/uninstall can safely reap them without touching foreign listeners.
marker_responds() {
  if [ -n "$prepared_node" ]; then
    local proof
    proof="$(NODE_OPTIONS= "$prepared_node" - "$dashboard_port" 2>/dev/null <<'JS'
const http = require("node:http");
const writeVerdict = (owned) => process.stdout.write(owned ? "owned" : "unowned");
const request = http.request({
  hostname: "127.0.0.1",
  port: Number(process.argv[2]),
  method: "HEAD",
  path: "/api/health",
  agent: false,
  signal: AbortSignal.timeout(2000),
}, (response) => {
  writeVerdict(response.statusCode < 400 && "x-claude-smart-dashboard" in response.headers);
  response.resume();
});
request.on("error", () => writeVerdict(false));
request.end();
JS
    )" || proof=""
    case "$proof" in
      owned) return 0 ;;
      unowned) return 1 ;;
    esac
  fi
  command -v curl >/dev/null 2>&1 || return 1
  curl -sfI --connect-timeout 1 --max-time 2 "http://127.0.0.1:$dashboard_port/api/health" 2>/dev/null \
    | grep -qi '^x-claude-smart-dashboard:'
}

# Kill a claude-smart dashboard listener currently holding the port. Gated by
# marker_responds so a foreign listener on the dashboard port is never killed.
stop_dashboard_listener() {
  marker_responds || return 0
  kill_recorded_dashboard
  claude_smart_reap_port_listeners_matching "$dashboard_port" \
    '*next-server*' \
    '*next*start*' \
    '*node*next*' \
    '*claude-smart*dashboard*' || true
}

# True if *something* is listening on the port, regardless of marker.
port_occupied() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$dashboard_port" 2>/dev/null && return 0
    # curl with -sfI against a 404/405 still indicates "something answered".
    # Use a connect-only probe as a secondary signal.
  fi
  (echo >"/dev/tcp/127.0.0.1/$dashboard_port") 2>/dev/null
}

resolve_next_bin() {
  if claude_smart_is_windows; then
    for candidate in "$DASHBOARD_DIR/node_modules/.bin/next.cmd" "$DASHBOARD_DIR/node_modules/.bin/next"; do
      [ -f "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    done
  else
    for candidate in "$DASHBOARD_DIR/node_modules/.bin/next" "$DASHBOARD_DIR/node_modules/.bin/next.cmd"; do
      [ -f "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    done
  fi
  return 1
}

prepared_dashboard_launchable() {
  local launcher
  [ -f "$DASHBOARD_DIR/.next/BUILD_ID" ] || return 1
  launcher="$(resolve_next_bin)" || return 1
  (cd "$DASHBOARD_DIR" && "$launcher" --version) >/dev/null 2>&1
}

prepared_dashboard_error="the prepared dashboard build or launcher is unusable; rerun npx claude-smart setup"

spawn_dashboard() {
  NEXT_BIN="$(resolve_next_bin || true)"
  if [ -z "$NEXT_BIN" ]; then
    reason="local Next.js binary is missing; run claude-smart install/update to restore dashboard dependencies"
    echo "[claude-smart] dashboard: $reason" >>"$LOG_FILE"
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi
  cd "$DASHBOARD_DIR"
  # Invoke Next directly so custom DASHBOARD_PORT values are honored
  # without relying on package.json's fixed start script.
  CLAUDE_SMART_SPAWN_KEEP_OUTPUT=1 claude_smart_spawn_detached "$NEXT_BIN" start -p "$dashboard_port" -H 127.0.0.1 >>"$LOG_FILE" 2>&1
  dash_pid=$!
  echo "$dash_pid" > "$PID_FILE"
}

wait_for_dashboard_marker() {
  attempts="$1"
  while [ "$attempts" -gt 0 ]; do
    marker_responds && return 0
    attempts=$((attempts - 1))
    sleep 1
  done
  marker_responds
}

case "$CMD" in
  preflight)
    [ "${CLAUDE_SMART_DASHBOARD_AUTOSTART:-1}" = "0" ] && exit 0
    if ! prepared_dashboard_launchable; then
      printf '%s\n' "$prepared_dashboard_error" >&2
      exit 1
    fi
    ;;
  start)
    if claude_smart_is_internal_invocation_env; then
      emit_ok; exit 0
    fi
    # Opt-out: users who don't want the dashboard long-lived can set
    # CLAUDE_SMART_DASHBOARD_AUTOSTART=0 in their environment.
    if [ "${CLAUDE_SMART_DASHBOARD_AUTOSTART:-1}" = "0" ]; then
      emit_ok; exit 0
    fi
    if [ ! -d "$DASHBOARD_DIR" ]; then emit_ok; exit 0; fi
    if marker_responds; then claude_smart_clear_dashboard_unavailable; emit_ok; exit 0; fi
    if ! claude_smart_service_lock "dashboard"; then
      for _ in 1 2 3 4 5; do
        if marker_responds; then
          claude_smart_clear_dashboard_unavailable
          emit_ok; exit 0
        fi
        sleep 0.2
      done
      emit_ok; exit 0
    fi
    trap 'claude_smart_service_unlock "dashboard"' EXIT
    if marker_responds; then claude_smart_clear_dashboard_unavailable; emit_ok; exit 0; fi
    if port_occupied; then
      echo "[claude-smart] dashboard: port $dashboard_port held by another process; skipping" >>"$LOG_FILE"
      emit_ok; exit 0
    fi
    if [ -f "$PLUGIN_ROOT/guide-prepared-runtime" ]; then
      if ! prepared_dashboard_launchable; then
        echo "[claude-smart] dashboard: $prepared_dashboard_error" >>"$LOG_FILE"
        claude_smart_write_dashboard_unavailable "$prepared_dashboard_error"
        emit_ok; exit 0
      fi
    else
      NPM_BIN=$(claude_smart_resolve_npm || true)
      if [ -z "$NPM_BIN" ] || ! "$NPM_BIN" --version >/dev/null 2>&1; then
        reason="npm is not on PATH; installer recovery scheduled; dashboard cannot start yet"
        echo "[claude-smart] dashboard: $reason; skipping" >>"$LOG_FILE"
        claude_smart_write_dashboard_unavailable "$reason"
        if [ "${CLAUDE_SMART_BOOTSTRAPPING:-}" != "1" ] && [ -x "$PLUGIN_ROOT/scripts/smart-install.sh" ]; then
          echo "[claude-smart] dashboard: npm is not on PATH; starting installer in background" >>"$LOG_FILE"
          # The continuation takes the same dashboard lock after Node repair.
          claude_smart_service_unlock "dashboard"
          trap - EXIT
          CLAUDE_SMART_SPAWN_KEEP_OUTPUT=1 claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1 \
            bash -c 'CLAUDE_SMART_INSTALL_PRIVATE_NODE_ONLY=1 bash "$1/smart-install.sh" &&
              exec bash "$1/dashboard-service.sh" start' _ "$HERE" \
            >>"$STATE_DIR/install.log" 2>&1 || true
        fi
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
          CLAUDE_SMART_SPAWN_KEEP_OUTPUT=1 claude_smart_spawn_detached bash "$HERE/dashboard-build.sh" >>"$LOG_FILE" 2>&1
        fi
        emit_ok; exit 0
      fi
    fi

    # Detach so the hook returns immediately. claude_smart_spawn_detached
    # picks the strongest primitive available:
    #   - Linux: setsid (puts child in its own session/group, pid==pgid).
    #   - macOS: python3 os.setsid + execvp (same effect as setsid).
    #   - Windows: nohup alone (no process groups; tree-kill via taskkill).
    # Caller-side `>>file 2>&1` redirection is honoured before the child
    # detaches, so per-OS log paths stay identical.
    if ! spawn_dashboard; then
      emit_ok; exit 0
    fi
    if wait_for_dashboard_marker 5; then
      claude_smart_clear_dashboard_unavailable
      emit_ok; exit 0
    fi
    if ! kill -0 "$dash_pid" 2>/dev/null; then
      sleep 2
      if port_occupied; then
        if marker_responds; then
          claude_smart_clear_dashboard_unavailable
        else
          claude_smart_write_dashboard_unavailable "dashboard process exited and port $dashboard_port is now held by a non-claude-smart listener; see $LOG_FILE"
        fi
      else
        echo "[claude-smart] dashboard: first start exited before readiness; retrying once" >>"$LOG_FILE"
        if spawn_dashboard; then
          if wait_for_dashboard_marker 5; then
            claude_smart_clear_dashboard_unavailable
          else
            claude_smart_write_dashboard_unavailable "dashboard process spawned but did not respond on http://127.0.0.1:$dashboard_port within 5s; see $LOG_FILE"
          fi
        fi
      fi
    else
      claude_smart_write_dashboard_unavailable "dashboard process spawned but did not respond on http://127.0.0.1:$dashboard_port within 5s; see $LOG_FILE"
    fi
    emit_ok
    ;;
  stop)
    kill_recorded_dashboard
    # Fallback: if a claude-smart dashboard is still responding on the port (e.g.,
    # was started outside this script, or the PGID kill missed because
    # the process wasn't the group leader) reap only dashboard-shaped listeners.
    # Gated on the marker header so we never touch a foreign listener.
    stop_dashboard_listener
    emit_ok
    ;;
  status)
    if marker_responds; then echo "running on http://localhost:$dashboard_port"; else echo "not running"; fi
    ;;
  *)
    emit_ok
    ;;
esac
