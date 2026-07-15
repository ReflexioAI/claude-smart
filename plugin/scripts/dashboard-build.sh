#!/usr/bin/env bash
# Build the Next.js dashboard ($PLUGIN_ROOT/dashboard) in two steps:
#   1. npm ci (skipped if node_modules exists and is newer than package.json
#      and package-lock.json)
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
claude_smart_prepend_node_bins

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
claude_smart_reexec_stable_plugin_root_if_needed "$PLUGIN_ROOT" "dashboard-build.sh" "$@"
DASHBOARD_DIR="$PLUGIN_ROOT/dashboard"

STATE_DIR="$HOME/.claude-smart"
LOG_FILE="$STATE_DIR/dashboard.log"
BUILD_PID_FILE="$STATE_DIR/dashboard-build.pid"
BUILD_LOCK_DIR="$STATE_DIR/dashboard-build.lock"
mkdir -p "$STATE_DIR"

log() { printf '[claude-smart] %s\n' "$1" >>"$LOG_FILE"; }

if [ ! -d "$DASHBOARD_DIR" ]; then
  log "dashboard build: no $DASHBOARD_DIR; nothing to do"
  exit 0
fi
NPM_BIN=$(claude_smart_resolve_npm || true)
if [ -z "$NPM_BIN" ] || ! "$NPM_BIN" --version >/dev/null 2>&1; then
  reason="npm is not on PATH; dashboard dependencies cannot be installed"
  log "dashboard build: $reason"
  claude_smart_write_dashboard_unavailable "$reason"
  exit 1
fi

# Collapse the PATH before invoking npm. In the detached SessionStart hook the
# PATH is ~7-9KB: prepend_node_bins pushes a (nonexistent) private-node dir to
# the front and repeated login-shell sourcing duplicates the whole list 3-4x.
# When npm's node process spawns a cmd.exe grandchild for a lifecycle/run script
# (`node -e ...`, `next build`), that oversized environment is truncated for the
# child, dropping the entries the script needs — nodejs and npm-appended
# node_modules/.bin — so bare `node`/`next` resolve to "not found" and the build
# aborts. Deduplicating (order-preserving) shrinks it back under the threshold;
# an identical small PATH is what makes an interactive build succeed. Anchor
# nodejs at the front too, so node survives even a residual truncation.
NPM_DIR=$(CDPATH= cd -- "$(dirname -- "$NPM_BIN")" && pwd)
PATH=$(printf '%s' "$NPM_DIR:$PATH" | awk -v RS=: -v ORS=: '!seen[$0]++')
export PATH="${PATH%:}"

# Atomic single-flight: mkdir is a single atomic syscall, so two concurrent
# builds (e.g., smart-install.sh and a SessionStart-driven dashboard-service.sh
# firing within milliseconds on first install) cannot both pass this check.
# BUILD_PID_FILE remains as status metadata for dashboard-open.sh and
# dashboard-service.sh to probe — it is written only after the lock is held
# and removed only by the lock holder.
# The pid file is written (below) only AFTER mkdir acquires the lock, so
# there is a brief window where the lock dir exists but the pid file does
# not. A peer arriving in that window must NOT treat the missing pid as a
# stale lock and steal it — doing so lets two builds run `npm ci` in the
# same dir, and each failure path wipes node_modules, clobbering the other
# (repeated "npm ci failed" across sessions, dashboard never builds).
# Disambiguate the two causes of "missing pid" by the lock dir's age:
#   - young lock, no pid  -> peer is mid-acquire; back off
#   - aged lock,  no pid  -> holder crashed before writing pid; reclaim
# LOCK_ACQUIRE_GRACE must exceed the mkdir->pid-write gap but stay well
# below a real build, so a genuinely crashed holder is still reclaimed.
LOCK_ACQUIRE_GRACE=30
if ! mkdir "$BUILD_LOCK_DIR" 2>/dev/null; then
  if claude_smart_pid_alive_file "$BUILD_PID_FILE"; then
    log "dashboard build: already in progress; skipping"
    exit 0
  fi
  if [ ! -s "$BUILD_PID_FILE" ]; then
    # Pid missing/empty: either a peer just acquired the lock and hasn't
    # written its pid yet, or a holder crashed in that window. Tell them
    # apart by how long the lock dir has existed.
    lock_now=$(date +%s 2>/dev/null || echo 0)
    # stat -c is GNU (Linux, Git Bash); stat -f is BSD (macOS). Try both.
    lock_born=$(stat -c %Y "$BUILD_LOCK_DIR" 2>/dev/null \
      || stat -f %m "$BUILD_LOCK_DIR" 2>/dev/null \
      || echo "$lock_now")
    if [ "$((lock_now - lock_born))" -lt "$LOCK_ACQUIRE_GRACE" ]; then
      log "dashboard build: peer acquiring lock; backing off"
      exit 0
    fi
    log "dashboard build: lock aged with no pid; reclaiming crashed holder"
  fi
  # Stale lock: pid present but dead, or an aged pid-less crash. Reclaim it;
  # if another process beats us to the reclaim, defer to them.
  rm -rf "$BUILD_LOCK_DIR"
  rm -f "$BUILD_PID_FILE"
  if ! mkdir "$BUILD_LOCK_DIR" 2>/dev/null; then
    log "dashboard build: lost race for stale lock; skipping"
    exit 0
  fi
fi
echo $$ > "$BUILD_PID_FILE"

release_lock() {
  rm -f "$BUILD_PID_FILE"
  rmdir "$BUILD_LOCK_DIR" 2>/dev/null || rm -rf "$BUILD_LOCK_DIR"
}
cleanup() {
  status=$?
  release_lock
  exit "${status:-0}"
}
on_interrupt() {
  rm -rf "$DASHBOARD_DIR/.next"
  rm -rf "$DASHBOARD_DIR/node_modules"
  release_lock
  log "dashboard build: interrupted; removed partial .next and node_modules"
  exit 130
}
trap cleanup EXIT
trap on_interrupt INT TERM

cd "$DASHBOARD_DIR"

# Cheap freshness check: skip reinstall when node_modules is newer than
# package.json and package-lock.json. Avoids re-downloading the dep tree
# on every SessionStart while still picking up version bumps when the
# plugin updates.
needs_install=1
if [ -d node_modules ] && [ node_modules -nt package.json ]; then
  if [ ! -f package-lock.json ] || [ node_modules -nt package-lock.json ]; then
    needs_install=0
  fi
fi
if [ "$needs_install" = "1" ]; then
  if [ -f package-lock.json ]; then
    install_cmd="ci"
  else
    install_cmd="install"
  fi
  log "dashboard build: running npm $install_cmd..."
  # --ignore-scripts: skip dependency lifecycle (pre/post)install scripts.
  # Some deps' postinstalls shell out to bare `node` (e.g. msw:
  # `node -e ...`), which fails with "node: not found" in the detached
  # SessionStart hook — npm.cmd runs (it finds node via its own %~dp0) but the
  # PATH it hands the cmd.exe grandchild doesn't reliably carry the nodejs dir,
  # so the script's bare `node` isn't resolvable and the whole `npm ci` aborts.
  # The dashboard builds and runs correctly without these scripts (sharp etc.
  # resolve via their prebuilt platform packages), so skipping them is both the
  # fix and a defensible hardening. npm's own newer default already blocks
  # unlisted install scripts; this makes that behaviour explicit and portable.
  # No --silent: on failure npm's real error must land in $LOG_FILE, otherwise
  # the dashboard-unavailable reason is useless and the failure is undiagnosable.
  if ! "$NPM_BIN" "$install_cmd" --ignore-scripts --no-fund --no-audit >>"$LOG_FILE" 2>&1; then
    rm -rf "$DASHBOARD_DIR/node_modules"
    reason="npm $install_cmd failed; removed partial node_modules; see $LOG_FILE"
    log "dashboard build: $reason"
    claude_smart_write_dashboard_unavailable "$reason"
    exit 1
  fi
fi

needs_build=1
if [ -d .next ] && [ .next -nt package.json ]; then
  needs_build=0
fi
if [ "$needs_build" = "1" ]; then
  log "dashboard build: running next build (this can take 1-2 min)..."
  if ! "$NPM_BIN" run build >>"$LOG_FILE" 2>&1; then
    rm -rf "$DASHBOARD_DIR/.next"
    reason="next build failed; see $LOG_FILE"
    log "dashboard build: $reason"
    claude_smart_write_dashboard_unavailable "$reason"
    exit 1
  fi
  claude_smart_clear_dashboard_unavailable
  log "dashboard build: complete"
else
  claude_smart_clear_dashboard_unavailable
  log "dashboard build: .next is up-to-date; skipping"
fi
