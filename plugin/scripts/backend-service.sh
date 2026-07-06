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

# True if /health returns 200. Reflexio's /health is a plain GET with no
# marker header, so we can't distinguish our backend from someone else's
# reflexio on the same port — if you run two reflexio instances on 8071
# you'll get collision regardless of what we do here.
backend_healthy() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -sf --connect-timeout 1 --max-time 1 -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null
}

embedding_healthy() {
  [ "${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-}" = "1" ] || return 0
  command -v curl >/dev/null 2>&1 || return 1
  curl -sf --connect-timeout 1 --max-time 1 -o /dev/null "http://127.0.0.1:$EMBEDDING_PORT/health" 2>/dev/null
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

# Best-effort port probe. curl catches responsive HTTP listeners; /dev/tcp
# catches other listeners where bash supports it. Used to avoid stomping on
# a foreign listener with a failed-to-start uvicorn.
port_occupied() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$PORT" 2>/dev/null && return 0
  fi
  (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

# List PIDs listening on $1 without lsof, for Windows git-bash where lsof is
# absent. Parses `netstat -ano` (the Windows netstat, on PATH under git-bash):
# columns are Proto, Local, Foreign, State, PID; we want LISTENING rows whose
# local address ends in :$port. Empty if netstat is unavailable or nothing
# listens.
port_pids_netstat() {
  command -v netstat >/dev/null 2>&1 || return 0
  netstat -ano 2>/dev/null | awk -v p=":$1\$" '
    $1 ~ /^TCP/ && $2 ~ p && $4 == "LISTENING" { print $5 }
  ' | sort -u
}

# Command line for a Windows PID via tasklist, for cmdline pattern matching
# when ps/lsof are unavailable. Prints the image name (best available proxy
# for the command on Windows) or empty.
pid_cmdline_win() {
  command -v tasklist >/dev/null 2>&1 || return 0
  tasklist /FI "PID eq $1" /FO CSV /NH 2>/dev/null \
    | awk -F'","' 'NR==1 { gsub(/"/,"",$1); print $1 }'
}

# Reap listeners still holding the given port after the PID file kill
# when their command line matches one of the supplied patterns. Filters
# by cmdline so we don't knock over an
# unrelated service a user has bound there — symmetric with start's
# refusal to stomp on a foreign listener. Silent on failure.
reap_port_listeners() {
  port="${1:-$PORT}"
  shift || true
  [ "$#" -eq 0 ] && return 0
  if command -v lsof >/dev/null 2>&1; then
    candidates=$(lsof -ti:"$port" 2>/dev/null) || candidates=""
  else
    candidates=$(port_pids_netstat "$port")
  fi
  [ -z "$candidates" ] && return 0
  ours=""
  for pid in $candidates; do
    cmdline=""
    if command -v ps >/dev/null 2>&1; then
      cmdline=$(ps -p "$pid" -o command= 2>/dev/null || true)
    fi
    # Git Bash ships `ps` but it may not support `-o command=` or resolve a
    # native PID from netstat, leaving cmdline empty — fall back to tasklist.
    if [ -z "$cmdline" ]; then
      cmdline=$(pid_cmdline_win "$pid")
    fi
    for pattern in "$@"; do
      if [[ "$cmdline" == $pattern ]]; then
        ours="$ours $pid"
        break
      fi
    done
  done
  [ -z "$ours" ] && return 0
  if command -v taskkill >/dev/null 2>&1 && ! command -v lsof >/dev/null 2>&1; then
    # Windows: no POSIX signals to these PIDs; force-kill directly.
    for pid in $ours; do taskkill /F /PID "$pid" >/dev/null 2>&1 || true; done
    return 0
  fi
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

# Describe what (if anything) is currently listening on $1. Returns
# "<command> (pid <pid>)" or empty if the port is free or lsof is
# unavailable. Used to make port-conflict log lines diagnosable.
port_holder() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -i:"$1" -sTCP:LISTEN -P -n 2>/dev/null \
      | awk 'NR==2 {print $1" (pid "$2")"; exit}'
    return 0
  fi
  pid=$(port_pids_netstat "$1" | head -n1)
  [ -z "$pid" ] && return 0
  printf '%s (pid %s)' "$(pid_cmdline_win "$pid")" "$pid"
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

# Full shutdown: kill the recorded process group (if any) then sweep
# both the backend port and the embedding-service port for surviving
# reflexio listeners. Used by both `stop` and the opt-in `session-end`
# path so a stale/missing PID file doesn't produce a silent no-op, and
# so a stale embedding service on EMBEDDING_PORT doesn't block the next
# fresh boot (e.g. when codex-hook.js falls back to 8072 for the main
# backend because 8071 is held by another app).
full_stop() {
  if [ -f "$PID_FILE" ]; then
    kill_group "$(cat "$PID_FILE" 2>/dev/null)"
    rm -f "$PID_FILE"
  fi
  reap_port_listeners "$PORT" '*reflexio*' '*uvicorn*'
  if [ -n "${EMBEDDING_PORT:-}" ] && [ "$EMBEDDING_PORT" != "$PORT" ]; then
    reap_port_listeners "$EMBEDDING_PORT" \
      '*reflexio.server.llm.embedding_service:app*' \
      '*reflexio*embedding_service*'
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
      embedding_healthy || log_embedding_degraded
      emit_ok; exit 0
    fi
    if port_occupied; then
      # is_our_backend_running already ruled out a healthy Reflexio backend, so
      # the holder is either our own wedged/unhealthy backend (reap + retry) or
      # a foreign process (surface and skip, never stomp it).
      holder="$(port_holder "$PORT" 2>/dev/null || true)"
      case "$holder" in
        *reflexio*|*uvicorn*|*python*)
          claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
            "[claude-smart] backend: stale unhealthy holder on port $PORT (${holder:-unknown}); reaping and retrying"
          reap_port_listeners "$PORT" '*reflexio*' '*uvicorn*' '*python*'
          ;;
      esac
      if port_occupied; then
        holder="$(port_holder "$PORT" 2>/dev/null || true)"
        msg="[claude-smart] backend: port $PORT held by another process${holder:+ ($holder)}; skipping start"
        claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "$msg"
        echo "$msg" >&2
        echo "Free port $PORT (or stop the process above) and run /claude-smart:restart again." >&2
        emit_ok; exit 0
      fi
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
    if [ -d "$PLUGIN_ROOT/vendor/reflexio/reflexio" ]; then
      vendor_pythonpath="$PLUGIN_ROOT/vendor/reflexio"
      pythonpath_sep=":"
      if claude_smart_is_windows; then
        # Native Windows Python expects ;-separated Windows-style paths in
        # PYTHONPATH; MSYS does not auto-convert arbitrary env vars.
        pythonpath_sep=";"
        vendor_pythonpath="$(claude_smart_to_windows_path "$vendor_pythonpath")"
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
      if ! wait_for_health embedding_healthy 5 3; then
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
    elif backend_healthy; then
      if embedding_healthy; then
        echo "running on http://localhost:$PORT"
      else
        echo "running on http://localhost:$PORT (embedding degraded on http://127.0.0.1:$EMBEDDING_PORT)"
      fi
    else
      echo "not running"
    fi
    ;;
  *)
    emit_ok
    ;;
esac
