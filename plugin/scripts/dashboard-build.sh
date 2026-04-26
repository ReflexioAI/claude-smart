#!/usr/bin/env bash
# Build the Next.js dashboard ($PLUGIN_ROOT/dashboard) in two steps:
#   1. npm install (skipped if node_modules exists and is newer than
#      package.json)
#   2. npm run build (skipped if .next exists and is newer than
#      package.json)
#
# Designed to run detached from any hook so the multi-minute first-run
# cost never trips Claude Code's hook timeout. dashboard-service.sh
# spawns this on first SessionStart when .next is missing; the user can
# also invoke it manually for recovery.
#
# Concurrency: a build-pid file at $STATE_DIR/dashboard-build.pid is
# used to mark "build in progress" so dashboard-open.sh can surface a
# "still building, retry in ~1 minute" message instead of the generic
# .next-missing error. The pid file is removed on exit (success, fail,
# or interrupt).
#
# Partial-build safety: an INT/TERM trap wipes any half-written .next
# so dashboard-service.sh's "no .next → start build" probe stays honest.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
claude_smart_source_login_path

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
DASHBOARD_DIR="$PLUGIN_ROOT/dashboard"

STATE_DIR="$HOME/.claude-smart"
LOG_FILE="$STATE_DIR/dashboard.log"
BUILD_PID_FILE="$STATE_DIR/dashboard-build.pid"
mkdir -p "$STATE_DIR"

log() { printf '[claude-smart] %s\n' "$1" >>"$LOG_FILE"; }

if [ ! -d "$DASHBOARD_DIR" ]; then
  log "dashboard build: no $DASHBOARD_DIR; nothing to do"
  exit 0
fi
if ! command -v npm >/dev/null 2>&1; then
  log "dashboard build: npm not on PATH; cannot build"
  exit 1
fi

# Single-flight guard: if another build is running, exit cleanly.
if claude_smart_pid_alive_file "$BUILD_PID_FILE"; then
  log "dashboard build: already in progress; skipping"
  exit 0
fi
rm -f "$BUILD_PID_FILE"
echo $$ > "$BUILD_PID_FILE"

cleanup() {
  status=$?
  rm -f "$BUILD_PID_FILE"
  exit "${status:-0}"
}
on_interrupt() {
  rm -rf "$DASHBOARD_DIR/.next"
  rm -f "$BUILD_PID_FILE"
  log "dashboard build: interrupted; removed partial .next"
  exit 130
}
trap cleanup EXIT
trap on_interrupt INT TERM

cd "$DASHBOARD_DIR"

# Cheap freshness check: skip reinstall when node_modules is newer than
# package.json. Avoids re-downloading the dep tree on every SessionStart
# while still picking up version bumps when the plugin updates.
# Note: lockfile-only or next.config-only edits won't trigger a rebuild —
# bump package.json (or `rm -rf .next`) in that case.
needs_install=1
if [ -d node_modules ] && [ node_modules -nt package.json ]; then
  needs_install=0
fi
if [ "$needs_install" = "1" ]; then
  log "dashboard build: running npm install..."
  if ! npm install --silent --no-fund --no-audit >>"$LOG_FILE" 2>&1; then
    log "dashboard build: npm install failed; see $LOG_FILE"
    exit 1
  fi
fi

needs_build=1
if [ -d .next ] && [ .next -nt package.json ]; then
  needs_build=0
fi
if [ "$needs_build" = "1" ]; then
  log "dashboard build: running next build (this can take 1-2 min)..."
  if ! npm run build >>"$LOG_FILE" 2>&1; then
    rm -rf "$DASHBOARD_DIR/.next"
    log "dashboard build: next build failed; see $LOG_FILE"
    exit 1
  fi
  log "dashboard build: complete"
else
  log "dashboard build: .next is up-to-date; skipping"
fi
