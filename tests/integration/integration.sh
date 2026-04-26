#!/usr/bin/env bash
# Post-install runtime integration test for claude-smart.
#
# Simulates a fresh user environment: sandboxed HOME, no prior reflexio
# state, no prior claude-smart state. Exercises the two hook-driven
# long-lived services (backend on :8071, dashboard on :3001) and asserts
# they come up healthy.
#
# Assumes a working install environment: uv, node, npm, python3 already
# on PATH. The GitHub Actions matrix controls the node version; uv is
# installed by the workflow before this runs. For local use, bring your
# own toolchain.
#
# Usage:
#   bash tests/integration/integration.sh            # all stages
#   bash tests/integration/integration.sh setup      # single stage
#   bash tests/integration/integration.sh setup-hook
#   bash tests/integration/integration.sh backend
#   bash tests/integration/integration.sh dashboard
#   bash tests/integration/integration.sh cleanup
#
# Exit status: 0 on success, non-zero on any stage failure. Dumps the
# relevant service logs to stderr before exiting non-zero.

set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PLUGIN_ROOT="$REPO_ROOT/plugin"

BACKEND_PORT=8071
DASHBOARD_PORT=3001

# Sandbox HOME so real ~/.reflexio, ~/.claude-smart, ~/.claude stay
# untouched. Created once, reused across stages within a single run.
if [ -z "${CLAUDE_SMART_INTEG_HOME:-}" ]; then
  INTEG_HOME="$(mktemp -d -t claude-smart-integration.XXXXXX)"
  export CLAUDE_SMART_INTEG_HOME="$INTEG_HOME"
else
  INTEG_HOME="$CLAUDE_SMART_INTEG_HOME"
  # Caller-supplied path may not exist yet (fresh env, typo in reuse).
  # Create it before exporting so later log redirects don't fail early.
  mkdir -p "$INTEG_HOME"
fi
export HOME="$INTEG_HOME"

# Let the plugin scripts resolve their own root without the hooks.json
# env var; smart-install.sh doesn't read CLAUDE_PLUGIN_ROOT but the
# SessionStart hooks do. Exporting it here documents intent and keeps
# any future script that honors it pointed at this checkout.
export CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT"

log()  { printf '[integration] %s\n' "$*" >&2; }
# Always try to stop services and dump logs on any failure. Cleanup on
# success runs as a normal stage so we can still see the logs if the
# trap path fires.
fail() {
  printf '[integration] FAIL: %s\n' "$*" >&2
  dump_logs
  stage_cleanup
  exit 1
}

# Keep ERR trap for unexpected failures (e.g., unhandled command failures)
on_error() {
  local rc=$?
  dump_logs
  stage_cleanup
  exit "$rc"
}
trap on_error ERR


dump_logs() {
  for f in "$HOME/.claude-smart/backend.log" "$HOME/.claude-smart/dashboard.log"; do
    if [ -f "$f" ]; then
      printf '\n===== %s =====\n' "$f" >&2
      cat "$f" >&2 || true
    fi
  done
}

# Poll a URL up to $1 seconds for HTTP 2xx. Returns 0 on first success.
poll_200() {
  local url="$1" timeout="$2"
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if curl -sf -o /dev/null "$url" 2>/dev/null; then return 0; fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

# Poll a URL up to $1 seconds for a specific response header. Returns 0
# when the header is present in the response. Used for the dashboard
# marker check so a foreign listener on :3001 doesn't pass the integration.
poll_header() {
  local url="$1" header="$2" timeout="$3"
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if curl -sfI "$url" 2>/dev/null | grep -qi "^${header}:"; then return 0; fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

stage_setup() {
  log "setup: sandbox HOME=$HOME"
  if ! command -v claude >/dev/null 2>&1; then
    fail "claude CLI not on PATH — install via 'npm install -g @anthropic-ai/claude-code'"
  fi
  # Plugin subcommands are local-config only, but the CLI may probe for
  # auth on first launch; a dummy key prevents a hang at a login prompt.
  export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-dummy-for-plugin-install}"

  log "setup: claude plugin marketplace add $REPO_ROOT"
  if ! claude plugin marketplace add "$REPO_ROOT" >"$HOME/marketplace-add.log" 2>&1; then
    cat "$HOME/marketplace-add.log" >&2 || true
    fail "claude plugin marketplace add failed"
  fi

  # `claude plugin install` only writes local config in headless mode — it
  # does NOT fire any lifecycle hooks (there is no "Setup" event in the
  # documented set). We still run it so the plugin is registered the way a
  # real user's machine would have it; smart-install.sh is invoked in the
  # next stage as a CI-only workaround. See upstream #11240.
  log "setup: claude plugin install claude-smart@reflexioai"
  if ! claude plugin install claude-smart@reflexioai >"$HOME/plugin-install.log" 2>&1; then
    cat "$HOME/plugin-install.log" >&2 || true
    fail "claude plugin install failed"
  fi
  log "setup: ok"
}

stage_setup_hook() {
  # CI-only workaround. Claude Code has no documented Setup/Install
  # lifecycle hook, so `claude plugin install` never runs smart-install.sh
  # in any environment (upstream anthropics/claude-code#11240). We invoke
  # it here directly to produce dashboard/.next, which dashboard-service.sh
  # refuses to build in the foreground. Remove this stage once upstream
  # ships a headless Setup trigger.
  log "setup-hook: running plugin/scripts/smart-install.sh"
  if ! bash "$PLUGIN_ROOT/scripts/smart-install.sh" >"$HOME/smart-install.log" 2>&1; then
    cat "$HOME/smart-install.log" >&2 || true
    fail "smart-install.sh failed"
  fi
  if [ -f "$HOME/.claude-smart/install-failed" ]; then
    cat "$HOME/.claude-smart/install-failed" >&2 || true
    fail "smart-install.sh wrote install-failed marker"
  fi
  # smart-install.sh now spawns dashboard-build.sh detached so the install
  # hook returns immediately. Wait up to 10 minutes for the build pid file
  # to clear AND .next/ to appear before failing.
  local build_pid_file="$HOME/.claude-smart/dashboard-build.pid"
  local i=0
  local timeout=600
  log "setup-hook: waiting for detached dashboard build (up to ${timeout}s)"
  while [ "$i" -lt "$timeout" ]; do
    if [ ! -e "$build_pid_file" ] && [ -d "$PLUGIN_ROOT/dashboard/.next" ]; then
      break
    fi
    sleep 2
    i=$((i + 2))
  done
  if [ -e "$build_pid_file" ] || [ ! -d "$PLUGIN_ROOT/dashboard/.next" ]; then
    cat "$HOME/smart-install.log" >&2 || true
    [ -f "$HOME/.claude-smart/dashboard.log" ] && cat "$HOME/.claude-smart/dashboard.log" >&2 || true
    fail "detached dashboard build did not finish within ${timeout}s (pid file present or .next missing)"
  fi
  log "setup-hook: ok"
}

stage_backend() {
  log "backend: starting"
  bash "$PLUGIN_ROOT/scripts/backend-service.sh" start >/dev/null
  log "backend: polling http://127.0.0.1:$BACKEND_PORT/health (20s)"
  if ! poll_200 "http://127.0.0.1:$BACKEND_PORT/health" 20; then
    fail "backend /health did not return 200 within 20s"
  fi
  log "backend: ok"
}

stage_dashboard() {
  log "dashboard: starting"
  bash "$PLUGIN_ROOT/scripts/dashboard-service.sh" start >/dev/null
  log "dashboard: polling http://127.0.0.1:$DASHBOARD_PORT/api/health for x-claude-smart-dashboard header (30s)"
  if ! poll_header "http://127.0.0.1:$DASHBOARD_PORT/api/health" "x-claude-smart-dashboard" 30; then
    fail "dashboard marker header not present within 30s"
  fi
  log "dashboard: ok"
}

stage_cleanup() {
  log "cleanup: stopping services (best-effort)"
  bash "$PLUGIN_ROOT/scripts/dashboard-service.sh" stop >/dev/null 2>&1 || true
  bash "$PLUGIN_ROOT/scripts/backend-service.sh"   stop >/dev/null 2>&1 || true
}

CMD="${1:-all}"
case "$CMD" in
  setup)      stage_setup ;;
  setup-hook) stage_setup_hook ;;
  backend)    stage_backend ;;
  dashboard)  stage_dashboard ;;
  cleanup)    stage_cleanup ;;
  all)
    stage_setup
    stage_setup_hook
    stage_backend
    stage_dashboard
    stage_cleanup
    log "all stages passed"
    ;;
  *) fail "unknown stage: $CMD (expected: setup|setup-hook|backend|dashboard|cleanup|all)" ;;
esac
