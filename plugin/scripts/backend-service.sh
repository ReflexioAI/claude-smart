#!/usr/bin/env bash
# Auto-start the reflexio FastAPI backend (port 8071) if it's not already
# running. Mirrors dashboard-service.sh: detached spawn, returns immediately
# so the SessionStart hook doesn't block the session.
#
# Subcommands:
#   start         probe /health; if nothing we recognize is on the port,
#                 spawn the prepared venv's `reflexio services start --only
#                 backend --no-reload` detached. Polls /health briefly so first
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
CMD="${1:-start}"
if [ "$CMD" != "session-end" ]; then
  claude_smart_source_login_path
fi
claude_smart_prepend_astral_bins
claude_smart_source_reflexio_env

PORT=8071
EMBEDDING_PORT="${EMBEDDING_PORT:-8072}"
# Pass through to `reflexio services start/stop` so the spawned backend
# binds to PORT instead of reflexio's library default (8081).
export BACKEND_PORT="$PORT"
export EMBEDDING_PORT

# Run claude-smart's backend under its own org id. The reflexio no-auth resolver
# reads REFLEXIO_DEFAULT_ORG_ID, which scopes the request org -> config file
# name (config_<org>.json) and SQLite data. Hard-set (not :-defaulted) so a
# value in the shared ~/.reflexio/.env can't drag claude-smart back onto
# "self-host-org" and collide with the enterprise self-host backend over
# config_self-host-org.json. Must stay in sync with cli.py's config path.
export REFLEXIO_DEFAULT_ORG_ID="claude-smart"

# Load claude-smart's env from ~/.claude-smart/.env instead of the reflexio
# default ~/.reflexio/.env, so an OSS reflexio backend's .env on this machine
# can't leak in. The data dir is independent (LOCAL_STORAGE_PATH), so both still
# share ~/.reflexio/data. Must stay in sync with env_config.CLAUDE_SMART_ENV_PATH and
# the source path in _lib.sh (claude_smart_source_reflexio_env).
export REFLEXIO_ENV_FILE="$HOME/.claude-smart/.env"

# Default: route extraction through the active host CLI + ONNX embedder
# so claude-smart works without any LLM API key. Users can opt out by
# pre-exporting these to 0.
export CLAUDE_SMART_USE_LOCAL_CLI="${CLAUDE_SMART_USE_LOCAL_CLI:-1}"
export CLAUDE_SMART_USE_LOCAL_EMBEDDING="${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-1}"
if [ "${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-}" = "1" ]; then
  export REFLEXIO_EMBEDDING_PROVIDER="${REFLEXIO_EMBEDDING_PROVIDER:-local_service}"
  export REFLEXIO_EMBEDDING_SERVICE_URL="${REFLEXIO_EMBEDDING_SERVICE_URL:-http://127.0.0.1:$EMBEDDING_PORT}"
fi
# The backend can be spawned from contexts whose PATH lacks the host
# CLI dir (commonly ~/.local/bin or /opt/homebrew/bin). Pin the CLI
# explicitly if we can resolve it from our own (post-login-path) PATH.
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
claude_smart_reexec_stable_plugin_root_if_needed "$PLUGIN_ROOT" "backend-service.sh" "$@"
PLUGIN_ROOT_CANONICAL="$(cd "$PLUGIN_ROOT" 2>/dev/null && pwd -P || printf '%s\n' "$PLUGIN_ROOT")"
VENDORED_REFLEXIO="$PLUGIN_ROOT_CANONICAL/vendor/reflexio"
VENDORED_REFLEXIO_FOR_PYTHON="$VENDORED_REFLEXIO"
if claude_smart_is_windows; then
  VENDORED_REFLEXIO_FOR_PYTHON="$(claude_smart_to_windows_path "$VENDORED_REFLEXIO")"
fi
export CLAUDE_SMART_REFLEXIO_VENDOR_ROOT="$VENDORED_REFLEXIO_FOR_PYTHON"

if [ -z "${CLAUDE_SMART_CLI_PATH:-}" ]; then
  if [ "${CLAUDE_SMART_HOST:-claude-code}" = "opencode" ]; then
    # Preserve Reflexio's Claude CLI provider contract while routing
    # generation through the user's authenticated OpenCode setup. The bridge
    # resolves opencode from CLAUDE_SMART_OPENCODE_PATH or PATH; don't require
    # Git Bash's PATH to match the installer shell's PATH here.
    claude_smart_prepend_node_bins
    CLAUDE_SMART_CLI_PATH="$(claude_smart_opencode_compat_path "$PLUGIN_ROOT")"
    export CLAUDE_SMART_CLI_PATH
  elif [ "${CLAUDE_SMART_HOST:-claude-code}" = "codex" ]; then
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

emit_ok() { claude_smart_emit_continue; }

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
    "`claude`, Codex needs `codex`, and OpenCode needs `opencode`. "
    "Then run `/claude-smart:restart`."
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

# True if /health returns 200, regardless of runtime identity. Used only for
# generic liveness; ownership checks inspect the listener process.
backend_healthy() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -sf --connect-timeout 1 --max-time 1 -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null
}

wait_for_health() {
  probe_fn="$1"
  attempts="$2"
  interval="$3"
  while [ "$attempts" -gt 0 ]; do
    "$probe_fn" && return 0
    attempts=$((attempts - 1))
    sleep "$interval"
  done
  "$probe_fn"
}

log_embedding_degraded() {
  claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
    "[claude-smart] backend: Embedding service did not become healthy on port $EMBEDDING_PORT; semantic retrieval remains unavailable until the local embedding daemon recovers"
}

# Best-effort port probe. curl catches responsive HTTP listeners; /dev/tcp
# catches other listeners where bash supports it. Used to avoid stomping on
# a foreign listener with a failed-to-start uvicorn.
port_occupied() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$PORT" 2>/dev/null && return 0
  fi
  (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

port_listener_pids() {
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -t -i ":$1" -sTCP:LISTEN 2>/dev/null || true
}

pid_details() {
  pid="$1"
  {
    ps eww -p "$pid" -o command= 2>/dev/null \
      || ps -p "$pid" -o command= 2>/dev/null \
      || true
    if command -v lsof >/dev/null 2>&1; then
      lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n/cwd=/p' || true
    fi
  } | tr '\n' ' '
}

looks_like_claude_smart_backend_pid() {
  pid="$1"
  text="$(pid_details "$pid")"
  [ -n "$text" ] || return 1
  case "$text" in
    *CLAUDE_SMART_REFLEXIO_VENDOR_ROOT=*|*CLAUDE_SMART_USE_LOCAL_CLI=1*|*CLAUDE_SMART_USE_LOCAL_EMBEDDING=1*|*".claude-smart/.env"*|*"reflexioai/claude-smart"*|*"reflexioai\\claude-smart"*|*"local-agent-mode-sessions"*|*"$PLUGIN_ROOT_CANONICAL"*|*"$HOME/.reflexio/plugin-root"*)
      return 0
      ;;
  esac
  return 1
}

pid_uses_current_vendor_root() {
  pid="$1"
  text="$(pid_details "$pid")"
  [ -n "$text" ] || return 1
  case "$text" in
    *"CLAUDE_SMART_REFLEXIO_VENDOR_ROOT=$VENDORED_REFLEXIO"*|*"CLAUDE_SMART_REFLEXIO_VENDOR_ROOT=$VENDORED_REFLEXIO_FOR_PYTHON"*|*"PYTHONPATH=$VENDORED_REFLEXIO"*|*"PYTHONPATH=$VENDORED_REFLEXIO_FOR_PYTHON"*|*"$VENDORED_REFLEXIO"*|*"$VENDORED_REFLEXIO_FOR_PYTHON"*)
      return 0
      ;;
  esac
  return 1
}

backend_owned_by_current_vendor() {
  backend_healthy || return 1
  pids="$(port_listener_pids "$PORT")"
  for pid in $pids; do
    if looks_like_claude_smart_backend_pid "$pid" && pid_uses_current_vendor_root "$pid"; then
      return 0
    fi
  done
  return 1
}

backend_owned_by_stale_claude_smart() {
  pids="$(port_listener_pids "$PORT")"
  for pid in $pids; do
    if looks_like_claude_smart_backend_pid "$pid" && ! pid_uses_current_vendor_root "$pid"; then
      return 0
    fi
  done
  return 1
}

embedding_owned_by_current_vendor() {
  [ "${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-}" = "1" ] || return 0
  pids="$(port_listener_pids "$EMBEDDING_PORT")"
  for pid in $pids; do
    if looks_like_claude_smart_backend_pid "$pid" && pid_uses_current_vendor_root "$pid"; then
      return 0
    fi
  done
  return 1
}

verify_bundled_reflexio_import() {
  python_bin="$1"
  pythonpath="$2"
  vendor_root_for_python="$3"
  [ -d "$VENDORED_REFLEXIO/reflexio" ] || {
    echo "bundled Reflexio package not found at $VENDORED_REFLEXIO" >&2
    return 1
  }
  PYTHONPATH="$pythonpath" "$python_bin" - "$vendor_root_for_python" <<'PY'
from pathlib import Path
import sys

vendor = Path(sys.argv[1]).resolve()
try:
    import reflexio
except Exception as exc:
    print(f"failed to import bundled reflexio from {vendor}: {exc}", file=sys.stderr)
    raise SystemExit(1)

module = Path(reflexio.__file__).resolve()
try:
    module.relative_to(vendor)
except ValueError:
    print(
        f"reflexio import resolved outside bundled vendor: {module} (expected under {vendor})",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

# True only if the recorded PID is alive, /health responds, and the listener
# process is using this package's bundled Reflexio import path.
is_our_backend_running() {
  if [ -f "$PID_FILE" ]; then
    pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      if command -v curl >/dev/null 2>&1; then
        backend_healthy && pid_uses_current_vendor_root "$pid" && return 0
      else
        pid_uses_current_vendor_root "$pid" && return 0
      fi
    fi
  fi
  # Recover from a missing PID file only when the listener proves it belongs
  # to the current vendored Reflexio package. A generic healthy service is not
  # enough.
  backend_owned_by_current_vendor && return 0
  return 1
}

stop_port_pids() {
  pids="$1"
  [ -n "$pids" ] || return 0
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true
  sleep 1
  remaining=""
  for pid in $pids; do
    kill -0 "$pid" 2>/dev/null && remaining="$remaining $pid"
  done
  [ -z "$remaining" ] && return 0
  # shellcheck disable=SC2086
  kill -KILL $remaining 2>/dev/null || true
}

stop_backend_listener_if_owned() {
  pids="$(port_listener_pids "$PORT")"
  [ -n "$pids" ] || return 1
  ours=""
  for pid in $pids; do
    if looks_like_claude_smart_backend_pid "$pid"; then
      ours="$ours $pid"
    fi
  done
  [ -n "$ours" ] || return 1
  stop_port_pids "$ours"
}

stop_stale_backend_listener_if_owned() {
  backend_owned_by_stale_claude_smart || return 1
  stale=""
  for pid in $(port_listener_pids "$PORT"); do
    if looks_like_claude_smart_backend_pid "$pid" && ! pid_uses_current_vendor_root "$pid"; then
      stale="$stale $pid"
    fi
  done
  [ -n "$stale" ] || return 1
  stop_port_pids "$stale"
}

stop_stale_embedding_listener_if_owned() {
  [ "${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-}" = "1" ] || return 0
  pids="$(port_listener_pids "$EMBEDDING_PORT")"
  [ -n "$pids" ] || return 0
  ours=""
  for pid in $pids; do
    if looks_like_claude_smart_backend_pid "$pid" && ! pid_uses_current_vendor_root "$pid"; then
      ours="$ours $pid"
    fi
  done
  [ -n "$ours" ] || return 0
  stop_port_pids "$ours"
}

# Describe what (if anything) is currently listening on $1. Returns
# "<command> (pid <pid>)" or empty if the port is free or lsof is
# unavailable. Used to make port-conflict log lines diagnosable.
port_holder() {
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -i:"$1" -sTCP:LISTEN -P -n 2>/dev/null \
    | awk 'NR==2 {print $1" (pid "$2")"; exit}'
}

ensure_vendored_reflexio_active() {
  vendor="$PLUGIN_ROOT/vendor/reflexio"
  [ -f "$vendor/pyproject.toml" ] || return 0
  plugin_python="$(claude_smart_plugin_python "$PLUGIN_ROOT")"
  [ -x "$plugin_python" ] || return 0
  if "$plugin_python" - "$vendor/pyproject.toml" <<'PY' >/dev/null 2>&1; then
import importlib.metadata
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
if not match:
    raise SystemExit(1)
expected = match.group(1)
try:
    installed = importlib.metadata.version("reflexio-ai")
except importlib.metadata.PackageNotFoundError:
    raise SystemExit(1)
raise SystemExit(0 if installed == expected else 1)
PY
    return 0
  fi
  claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
    "[claude-smart] backend: repairing vendored Reflexio install before start"
  uv pip install --project "$PLUGIN_ROOT" --python "$plugin_python" --quiet --reinstall --no-deps "$vendor" >&2
}

# Full shutdown: kill the recorded process group (if any) then sweep stale
# claude-smart-owned backend/embedding listeners. Foreign services on the same
# ports are left alone.
full_stop() {
  if [ -f "$PID_FILE" ]; then
    kill_group "$(cat "$PID_FILE" 2>/dev/null)"
    rm -f "$PID_FILE"
  fi
  stop_backend_listener_if_owned || true
  if [ -n "${EMBEDDING_PORT:-}" ] && [ "$EMBEDDING_PORT" != "$PORT" ]; then
    stop_stale_embedding_listener_if_owned || true
  fi
}

case "$CMD" in
  start)
    if claude_smart_is_internal_invocation_env; then
      emit_ok; exit 0
    fi
    if claude_smart_reflexio_url_is_remote; then
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "[claude-smart] backend: remote REFLEXIO_URL configured; skipping local backend start"
      emit_ok; exit 0
    fi
    # Opt-out: users who don't want the backend managed by the hook can
    # set CLAUDE_SMART_BACKEND_AUTOSTART=0.
    if [ "${CLAUDE_SMART_BACKEND_AUTOSTART:-1}" = "0" ]; then
      emit_ok; exit 0
    fi
    if is_our_backend_running; then
      embedding_owned_by_current_vendor || log_embedding_degraded
      emit_ok; exit 0
    fi
    if stop_stale_backend_listener_if_owned; then
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
        "[claude-smart] backend: replaced claude-smart backend on port $PORT that was not using bundled Reflexio at $VENDORED_REFLEXIO"
    fi
    stop_stale_embedding_listener_if_owned || true
    if port_occupied; then
      # is_our_backend_running already ruled out a healthy Reflexio backend.
      # Anything still holding the port would make uvicorn fail to bind, so
      # surface the holder instead of silently starting into a collision.
      holder="$(port_holder "$PORT" 2>/dev/null || true)"
      msg="[claude-smart] backend: port $PORT held by another process${holder:+ ($holder)}; skipping start"
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "$msg"
      echo "$msg" >&2
      echo "Free port $PORT (or stop the process above) and run /claude-smart:restart again." >&2
      emit_ok; exit 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
      if [ "${CLAUDE_SMART_BOOTSTRAPPING:-}" != "1" ] && [ -x "$PLUGIN_ROOT/scripts/smart-install.sh" ]; then
        claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "[claude-smart] backend: uv not on PATH; starting installer in background"
        CLAUDE_SMART_SPAWN_KEEP_OUTPUT=1 claude_smart_spawn_detached env CLAUDE_SMART_BOOTSTRAPPING=1 \
          bash "$PLUGIN_ROOT/scripts/smart-install.sh" \
          >>"$STATE_DIR/install.log" 2>&1 || true
      fi
      if ! command -v uv >/dev/null 2>&1; then
        claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "[claude-smart] backend: uv not on PATH; installer recovery scheduled; skipping"
        emit_ok; exit 0
      fi
    fi
    cd "$PLUGIN_ROOT"
    ensure_vendored_reflexio_active

    # Cap local interaction history to keep the SQLite store small for
    # claude-smart users. Reflexio's library defaults are much higher
    # (250k/50k) for server deployments; here we override only in the
    # claude-smart plugin context. Users can still override via env.
    export INTERACTION_CLEANUP_THRESHOLD="${INTERACTION_CLEANUP_THRESHOLD:-500}"
    export INTERACTION_CLEANUP_DELETE_COUNT="${INTERACTION_CLEANUP_DELETE_COUNT:-200}"

    # Keep plugin runtime data in ~/.reflexio even when the backend imports
    # Reflexio from an editable checkout inside a larger repo with its own
    # .env. python-dotenv respects pre-existing env vars, so this prevents a
    # parent REFLEXIO_LOG_DIR from sending claude-smart to unrelated configs.
    export REFLEXIO_LOG_DIR="${REFLEXIO_LOG_DIR:-$HOME}"

    # Force sqlite: the plugin venv ships only the open-source reflexio
    # package, which doesn't register the Supabase/Postgres storage
    # factories. If the user's ~/.reflexio/.env sets REFLEXIO_STORAGE to
    # something else (common when sharing the file with reflexio_ext),
    # the backend boots but crashes every request. load_dotenv() inside
    # the CLI respects pre-existing env vars, so exporting here wins
    # without touching the file on disk.
    export REFLEXIO_STORAGE="sqlite"

    backend_pythonpath="${PYTHONPATH:-}"
    if [ -d "$VENDORED_REFLEXIO/reflexio" ]; then
      vendor_pythonpath="$VENDORED_REFLEXIO"
      pythonpath_sep=":"
      if claude_smart_is_windows; then
        # Native Windows Python expects ;-separated Windows-style paths in
        # PYTHONPATH; MSYS does not auto-convert arbitrary env vars.
        pythonpath_sep=";"
        vendor_pythonpath="$VENDORED_REFLEXIO_FOR_PYTHON"
      fi
      backend_pythonpath="$vendor_pythonpath${backend_pythonpath:+$pythonpath_sep$backend_pythonpath}"
    fi

    # (nohup; no process groups). backend-log-runner.sh owns stdout/stderr
    # capture so process output cannot grow backend.log past its cap.
    #
    # --workers: reflexio defaults to 2 (zero-downtime worker recycling
    # for server deployments). For a single-user Claude Code plugin
    # that's pure overhead: ~1.1 GB extra RSS, periodic 5–10 s spawn
    # hiccups during worker rotation, and SQLite can't accept concurrent
    # writers anyway. Default to 1 here; opt in to N via
    # CLAUDE_SMART_BACKEND_WORKERS for power users running concurrent
    # Claude Code sessions or wanting zero-downtime recycling.
    workers="${CLAUDE_SMART_BACKEND_WORKERS:-1}"
    backend_python="$(claude_smart_plugin_python "$PLUGIN_ROOT")"
    if ! verify_bundled_reflexio_import "$backend_python" "$backend_pythonpath" "$VENDORED_REFLEXIO_FOR_PYTHON"; then
      reason="bundled Reflexio import preflight failed"
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "[claude-smart] backend: $reason"
      emit_start_failure "$reason"
      exit 0
    fi
    # Record the spawned pid, not a pgid sampled with ps. On POSIX,
    # setsid/python os.setsid make this pid the new process group leader;
    # sampling immediately can race and capture the caller's pgid instead.
    # On Windows, claude_smart_kill_tree translates the MSYS pid to WINPID.
    claude_smart_spawn_detached bash "$HERE/backend-log-runner.sh" \
      "$LOG_FILE" "$LOG_MAX_BYTES" -- \
      env PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}" \
      PYTHONPATH="$backend_pythonpath" \
      "$backend_python" -m reflexio.cli services start --only backend --no-reload --workers "$workers"
    svc_pid=$!
    echo "$svc_pid" > "$PID_FILE"

    # Give uvicorn about 20s to answer /health. The first boot after a fresh
    # checkout may be slower; if it catches up later, the next hook observes it.
    if wait_for_health backend_healthy 10 1; then
      # The Node installer waits 45s for this script. Each curl probe is also
      # bounded so a wedged listener cannot outlive the caller's timeout budget.
      if ! wait_for_health embedding_owned_by_current_vendor 5 3; then
        log_embedding_degraded
      fi
    else
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
    if claude_smart_reflexio_url_is_remote; then
      echo "remote configured at $REFLEXIO_URL"
    elif backend_owned_by_current_vendor; then
      if embedding_owned_by_current_vendor; then
        echo "running on http://localhost:$PORT (bundled Reflexio at $VENDORED_REFLEXIO)"
      else
        echo "running on http://localhost:$PORT (bundled Reflexio at $VENDORED_REFLEXIO; embedding degraded on http://127.0.0.1:$EMBEDDING_PORT)"
      fi
    elif backend_owned_by_stale_claude_smart; then
      echo "stale claude-smart backend on http://localhost:$PORT (not using bundled Reflexio at $VENDORED_REFLEXIO)"
    elif backend_healthy; then
      echo "foreign or ambiguous backend on http://localhost:$PORT (not managed by claude-smart)"
    else
      echo "not running"
    fi
    ;;
  *)
    emit_ok
    ;;
esac
