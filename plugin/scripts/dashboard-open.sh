#!/usr/bin/env bash
# Start backend + dashboard (idempotent), wait briefly for dashboard to come
# up, print statuses, then open the dashboard in the default browser.
#
# Exists so the /claude-smart:dashboard slash command can invoke a single
# plain `bash <script>` with no inline $(...) or ${...:-...} expansion in
# the command string — Claude Code's permission checker rejects those.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$HERE"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
claude_smart_source_login_path
claude_smart_prepend_node_bins

STATE_DIR="$HOME/.claude-smart"
BACKEND_LOG="$STATE_DIR/backend.log"
DASHBOARD_LOG="$STATE_DIR/dashboard.log"
DASHBOARD_UNAVAILABLE="$(claude_smart_dashboard_unavailable_marker)"
BACKEND_PORT="${BACKEND_PORT:-8071}"
DASHBOARD_PORT="${DASHBOARD_PORT:-${PORT:-3001}}"

# Capture start-command output so we can surface fatal errors (e.g. uv/npm
# missing, port collisions, .next not built) rather than silently swallow
# them. The service scripts themselves write to their own log files on
# successful detached spawn, so stdout/stderr here are normally empty.
backend_start_out=$(bash "$SCRIPTS/backend-service.sh" start 2>&1) || true
dashboard_start_out=$(bash "$SCRIPTS/dashboard-service.sh" start 2>&1) || true

# Poll both services for up to ~10s so a cold boot has time to come up.
backend_status="not running"
dashboard_status="not running"
for _ in 1 2 3 4 5 6 7 8 9 10; do
    backend_status=$(bash "$SCRIPTS/backend-service.sh" status)
    dashboard_status=$(bash "$SCRIPTS/dashboard-service.sh" status)
    if [ "$backend_status" != "not running" ] && [ "$dashboard_status" != "not running" ]; then
        break
    fi
    sleep 1
done

echo "backend:   $backend_status"
echo "dashboard: $dashboard_status"

# Print any "skipping" diagnostic the service scripts appended to their
# logs (uv/npm missing, port held, .next missing, etc.). Tail is cheap
# and gives the user something actionable instead of just "not running".
show_log_tail() {
    label="$1"
    log_path="$2"
    if [ -f "$log_path" ]; then
        tail=$(tail -n 20 "$log_path" 2>/dev/null || true)
        if [ -n "$tail" ]; then
            echo ""
            echo "--- $label log (last 20 lines: $log_path) ---"
            echo "$tail"
        fi
    else
        echo ""
        echo "[$label] no log at $log_path"
    fi
}

failed=0
if [ "$backend_status" = "not running" ]; then
    failed=1
    echo ""
    echo "ERROR: backend failed to start on http://localhost:$BACKEND_PORT"
    [ -n "$backend_start_out" ] && echo "$backend_start_out"
    show_log_tail "backend" "$BACKEND_LOG"
fi
if [ "$dashboard_status" = "not running" ]; then
    failed=1
    echo ""
    BUILD_PID_FILE="$STATE_DIR/dashboard-build.pid"
    if claude_smart_pid_alive_file "$BUILD_PID_FILE"; then
        echo "dashboard: still building (first-run cost, ~1-2 min). Re-run /claude-smart:dashboard in a minute."
    else
        echo "ERROR: dashboard failed to start on http://localhost:$DASHBOARD_PORT"
        [ -n "$dashboard_start_out" ] && echo "$dashboard_start_out"
        if [ -f "$DASHBOARD_UNAVAILABLE" ]; then
            echo ""
            echo "--- dashboard availability ($DASHBOARD_UNAVAILABLE) ---"
            cat "$DASHBOARD_UNAVAILABLE" 2>/dev/null || true
        fi
        show_log_tail "dashboard" "$DASHBOARD_LOG"
    fi
fi

if [ "$failed" = "1" ]; then
    echo ""
    echo "Not opening the browser because one or more services failed to start."
    exit 1
fi

URL="http://localhost:$DASHBOARD_PORT"
PY_BIN=$(claude_smart_resolve_python || true)
if [ -n "$PY_BIN" ] && "$PY_BIN" -m webbrowser "$URL"; then
    echo "Opened $URL"
elif command -v open >/dev/null 2>&1 && open "$URL"; then
    echo "Opened $URL"
elif command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL"; then
    echo "Opened $URL"
elif command -v powershell >/dev/null 2>&1 && powershell -NoProfile -Command "Start-Process '$URL'"; then
    echo "Opened $URL"
else
    echo "Dashboard is running at $URL"
fi
