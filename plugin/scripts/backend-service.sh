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

PORT="${BACKEND_PORT:-8071}"
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

plugin_version_from_root() {
  root="$1"
  version="$(
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      "$root/.codex-plugin/plugin.json" 2>/dev/null | head -n1
  )"
  if [ -z "$version" ]; then
    version="$(
      sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$root/package.json" 2>/dev/null | head -n1
    )"
  fi
  if [ -z "$version" ]; then
    version="$(
      sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$root/pyproject.toml" 2>/dev/null | head -n1
    )"
  fi
  printf '%s\n' "${version:-unknown}"
}

plugin_root_has_version_manifest() {
  root="$1"
  [ -f "$root/.codex-plugin/plugin.json" ] || [ -f "$root/package.json" ] || [ -f "$root/pyproject.toml" ]
}

PLUGIN_VERSION="$(plugin_version_from_root "$PLUGIN_ROOT_CANONICAL")"

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
backend_pid_key="$(
  printf '%s' "$PLUGIN_ROOT_CANONICAL" | cksum 2>/dev/null | awk '{print $1}' || true
)"
backend_pid_key="${backend_pid_key:-default}"
PID_FILE="$STATE_DIR/backend.$backend_pid_key.pid"
SHARED_PID_FILE="$STATE_DIR/backend.pid"
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

pid_details() {
  pid="$1"
  seen=" "
  depth=0
  while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$depth" -lt 6 ]; do
    case "$seen" in *" $pid "*) break ;; esac
    seen="$seen$pid "
    {
      printf 'pid=%s ' "$pid"
      claude_smart_pid_command "$pid" 2>/dev/null || true
      if command -v lsof >/dev/null 2>&1; then
        lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n/cwd=/p' || true
      fi
    }
    parent="$(claude_smart_pid_parent "$pid" 2>/dev/null || true)"
    [ -n "$parent" ] && [ "$parent" != "$pid" ] || break
    pid="$parent"
    depth=$((depth + 1))
  done | tr '\n' ' '
}

marked_backend_ancestor_pid() {
  pid="$1"
  seen=" "
  depth=0
  while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$depth" -lt 6 ]; do
    case "$seen" in *" $pid "*) break ;; esac
    seen="$seen$pid "
    cmdline="$(claude_smart_pid_command "$pid" | tr '\n' ' ' || true)"
    case "$cmdline" in
      *backend-python-runner.py*--claude-smart-backend=1*)
        printf '%s\n' "$pid"
        return 0
        ;;
    esac
    parent="$(claude_smart_pid_parent "$pid" 2>/dev/null || true)"
    [ -n "$parent" ] && [ "$parent" != "$pid" ] || break
    pid="$parent"
    depth=$((depth + 1))
  done
  return 1
}

stop_backend_pids() {
  pids="$1"
  [ -n "$pids" ] || return 0
  targets=""
  for pid in $pids; do
    target="$(marked_backend_ancestor_pid "$pid" 2>/dev/null || true)"
    for candidate in ${target:-} "$pid"; do
      [ -n "$candidate" ] || continue
      case " $targets " in *" $candidate "*) ;; *) targets="$targets $candidate" ;; esac
    done
  done
  for target in $targets; do
    kill_group "$target"
  done
}

looks_like_claude_smart_backend_pid() {
  pid="$1"
  text="$(pid_details "$pid")"
  [ -n "$text" ] || return 1
  case "$text" in
    *--claude-smart-backend=1*|*--claude-smart-reflexio-vendor-root=*|*--claude-smart-plugin-root=*|*"reflexioai/claude-smart"*|*"reflexioai\\claude-smart"*)
      return 0
      ;;
  esac
  for token in $text; do
    value="$(pid_token_path_entry "$token")"
    path_is_or_under_root "$PLUGIN_ROOT_CANONICAL" "$value" && return 0
  done
  return 1
}

is_claude_smart_backend_pid() {
  pid="$1"
  backend_pid_command_matches "$pid" && looks_like_claude_smart_backend_pid "$pid"
}

path_is_or_under_root() {
  root="$1"
  value="$2"
  [ -n "$root" ] && [ -n "$value" ] || return 1
  case "$value" in
    "$root"|"$root"/*|"$root"\\*) return 0 ;;
  esac
  return 1
}

pid_token_path_entry() {
  token="$1"
  value="${token#*=}"
  # PYTHONPATH can hold multiple roots; split path-list suffixes without
  # treating a Windows drive prefix like C:\... as a separator.
  case "$value" in
    *";"*) value="${value%%;*}" ;;
    [A-Za-z]:\\*) ;;
    *) value="${value%%:*}" ;;
  esac
  printf '%s\n' "$value"
}

pid_vendor_root() {
  pid="$1"
  text="$(pid_details "$pid")"
  [ -n "$text" ] || return 1
  for token in $text; do
    case "$token" in
      --claude-smart-reflexio-vendor-root=*)
        printf '%s\n' "${token#--claude-smart-reflexio-vendor-root=}"
        return 0
        ;;
      --claude-smart-plugin-root=*)
        printf '%s/vendor/reflexio\n' "${token#--claude-smart-plugin-root=}"
        return 0
        ;;
    esac
    value="$(pid_token_path_entry "$token")"
    case "$value" in
      */vendor/reflexio|*\\vendor\\reflexio)
        root="$(plugin_root_from_vendor_root "$value" 2>/dev/null || true)"
        if [ -n "$root" ] && plugin_root_has_version_manifest "$root"; then
          printf '%s\n' "$value"
          return 0
        fi
        ;;
    esac
  done
  return 1
}

plugin_root_from_vendor_root() {
  vendor="$1"
  case "$vendor" in
    */vendor/reflexio) printf '%s\n' "${vendor%/vendor/reflexio}"; return 0 ;;
    *\\vendor\\reflexio) printf '%s\n' "${vendor%\\vendor\\reflexio}"; return 0 ;;
  esac
  return 1
}

pid_plugin_root() {
  text="$(pid_details "$1")"
  for token in $text; do
    case "$token" in
      --claude-smart-plugin-root=*)
        printf '%s\n' "${token#--claude-smart-plugin-root=}"
        return 0
        ;;
    esac
  done
  vendor="$(pid_vendor_root "$1" 2>/dev/null || true)"
  if [ -n "$vendor" ]; then
    plugin_root_from_vendor_root "$vendor" && return 0
  fi
  for token in $text; do
    case "$token" in
      cwd=*)
        root="${token#cwd=}"
        if plugin_root_has_version_manifest "$root"; then
          printf '%s\n' "$root"
          return 0
        fi
        ;;
    esac
  done
  return 1
}

pid_plugin_version() {
  text="$(pid_details "$1")"
  for token in $text; do
    case "$token" in
      --claude-smart-version=*)
        printf '%s\n' "${token#--claude-smart-version=}"
        return 0
        ;;
    esac
  done
  root="$(pid_plugin_root "$1" 2>/dev/null || true)"
  [ -n "$root" ] || { printf '%s\n' "unknown"; return 0; }
  plugin_version_from_root "$root"
}

pid_uses_current_vendor_root() {
  pid="$1"
  text="$(pid_details "$pid")"
  [ -n "$text" ] || return 1
  for token in $text; do
    case "$token" in
      --claude-smart-plugin-root=*)
        value="${token#--claude-smart-plugin-root=}"
        path_is_or_under_root "$PLUGIN_ROOT_CANONICAL" "$value" && return 0
        ;;
      --claude-smart-reflexio-vendor-root=*)
        value="${token#--claude-smart-reflexio-vendor-root=}"
        path_is_or_under_root "$VENDORED_REFLEXIO" "$value" && return 0
        path_is_or_under_root "$VENDORED_REFLEXIO_FOR_PYTHON" "$value" && return 0
        ;;
      *)
        value="$(pid_token_path_entry "$token")"
        path_is_or_under_root "$VENDORED_REFLEXIO" "$value" && return 0
        path_is_or_under_root "$VENDORED_REFLEXIO_FOR_PYTHON" "$value" && return 0
        ;;
    esac
  done
  return 1
}

backend_version_core_parts() {
  version="$1"
  printf '%s\n' "$version" \
    | sed -n 's/^\([0-9][0-9]*\)\.\([0-9][0-9]*\)\.\([0-9][0-9]*\)$/\1 \2 \3/p'
}

backend_version_older_than() {
  actual_version_input="$1"
  target_version_input="$2"
  actual_core="$(backend_version_core_parts "$actual_version_input")"
  target_core="$(backend_version_core_parts "$target_version_input")"
  [ -n "$actual_core" ] && [ -n "$target_core" ] || return 1

  set -- $actual_core
  actual_major="$1"; actual_minor="$2"; actual_patch="$3"
  set -- $target_core
  target_major="$1"; target_minor="$2"; target_patch="$3"

  [ "$actual_major" -lt "$target_major" ] && return 0
  [ "$actual_major" -gt "$target_major" ] && return 1
  [ "$actual_minor" -lt "$target_minor" ] && return 0
  [ "$actual_minor" -gt "$target_minor" ] && return 1
  [ "$actual_patch" -lt "$target_patch" ] && return 0
  return 1
}

backend_pid_current_or_compatible() {
  pid="$1"
  pid_uses_current_vendor_root "$pid" && return 0
  [ "$PLUGIN_VERSION" = "unknown" ] && return 0
  running_version="$(pid_plugin_version "$pid")"
  [ "$running_version" = "unknown" ] && return 0
  ! backend_version_older_than "$running_version" "$PLUGIN_VERSION"
}

backend_pid_strictly_older_than_plugin() {
  pid="$1"
  [ "$PLUGIN_VERSION" != "unknown" ] || return 1
  pid_uses_current_vendor_root "$pid" && return 1
  running_version="$(pid_plugin_version "$pid")"
  [ "$running_version" != "unknown" ] || return 1
  backend_version_older_than "$running_version" "$PLUGIN_VERSION"
}

backend_listener_pids() {
  claude_smart_port_listener_pids "$PORT"
}

classify_backend_listener() {
  pids="$(backend_listener_pids || true)"
  [ -n "$pids" ] || { printf '%s\n' "free"; return 0; }
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid"; then
      printf '%s\n' "claude_smart"
      return 0
    fi
  done
  printf '%s\n' "foreign"
}

backend_owned_by_current_vendor() {
  backend_healthy || return 1
  pids="$(backend_listener_pids)"
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid" && pid_uses_current_vendor_root "$pid"; then
      return 0
    fi
  done
  return 1
}

backend_owned_by_stale_claude_smart() {
  pids="$(backend_listener_pids)"
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid" && backend_pid_strictly_older_than_plugin "$pid"; then
      return 0
    fi
  done
  return 1
}

backend_current_or_compatible() {
  backend_healthy || return 1
  pids="$(backend_listener_pids)"
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid" && backend_pid_current_or_compatible "$pid"; then
      return 0
    fi
  done
  return 1
}

embedding_owned_by_current_vendor() {
  [ "${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-}" = "1" ] || return 0
  pids="$(claude_smart_port_listener_pids "$EMBEDDING_PORT")"
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid" && pid_uses_current_vendor_root "$pid"; then
      return 0
    fi
  done
  return 1
}

embedding_current_or_compatible() {
  [ "${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-}" = "1" ] || return 0
  pids="$(claude_smart_port_listener_pids "$EMBEDDING_PORT")"
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid" && backend_pid_current_or_compatible "$pid"; then
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
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && backend_pid_command_matches "$pid"; then
      if command -v curl >/dev/null 2>&1; then
        backend_healthy && backend_pid_current_or_compatible "$pid" && return 0
      else
        backend_pid_current_or_compatible "$pid" && return 0
      fi
    fi
  fi
  # Recover from a missing PID file only when the listener proves it belongs
  # to a compatible claude-smart package. A generic healthy service is not
  # enough, and a known older claude-smart package is restarted later.
  backend_current_or_compatible && return 0
  return 1
}

stop_backend_listener_if_owned() {
  pids="$(backend_listener_pids)"
  [ -n "$pids" ] || return 1
  ours=""
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid" && { pid_uses_current_vendor_root "$pid" || backend_pid_strictly_older_than_plugin "$pid"; }; then
      ours="$ours $pid"
    fi
  done
  [ -n "$ours" ] || return 1
  stop_backend_pids "$ours"
}

stop_stale_backend_listener_if_owned() {
  backend_owned_by_stale_claude_smart || return 1
  stale=""
  for pid in $(backend_listener_pids); do
    if is_claude_smart_backend_pid "$pid" && backend_pid_strictly_older_than_plugin "$pid"; then
      stale="$stale $pid"
    fi
  done
  [ -n "$stale" ] || return 1
  stop_backend_pids "$stale"
}

stop_stale_embedding_listener_if_owned() {
  [ "${CLAUDE_SMART_USE_LOCAL_EMBEDDING:-}" = "1" ] || return 0
  pids="$(claude_smart_port_listener_pids "$EMBEDDING_PORT")"
  [ -n "$pids" ] || return 0
  ours=""
  for pid in $pids; do
    if is_claude_smart_backend_pid "$pid" && backend_pid_strictly_older_than_plugin "$pid"; then
      ours="$ours $pid"
    fi
  done
  [ -n "$ours" ] || return 0
  stop_backend_pids "$ours"
}

# Describe what (if anything) is currently listening on $1. Returns
# "<command> (pid <pid>)" or empty if the port is free or no platform probe is
# available. Used to make port-conflict log lines diagnosable.
port_holder() {
  claude_smart_port_holder "$1"
}

recorded_backend_pid() {
  [ -f "$PID_FILE" ] || return 1
  pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
  [ -n "$pid" ] || return 1
  printf '%s\n' "$pid"
}

shared_backend_pid() {
  [ -f "$SHARED_PID_FILE" ] || return 1
  pid=$(cat "$SHARED_PID_FILE" 2>/dev/null || echo "")
  [ -n "$pid" ] || return 1
  printf '%s\n' "$pid"
}

backend_pid_command_matches() {
  pid="$1"
  cmdline="$(claude_smart_pid_command "$pid" | tr '\n' ' ' || true)"
  [ -n "$cmdline" ] || return 1
  case "$cmdline" in
    *backend-python-runner.py*--claude-smart-backend=1*services*start*|*reflexio.cli*services*start*|*reflexio.server*|*uvicorn*reflexio*|*reflexio*uvicorn*) return 0 ;;
    *) return 1 ;;
  esac
}

recorded_backend_pid_alive() {
  pid="$(recorded_backend_pid 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  backend_pid_command_matches "$pid"
}

backend_pid_is_own_recorded() {
  pid="$1"
  recorded="$(recorded_backend_pid 2>/dev/null || true)"
  [ -n "$recorded" ] && [ "$pid" = "$recorded" ]
}

pid_cwd_matches_current_plugin_root() {
  pid="$1"
  command -v lsof >/dev/null 2>&1 || return 1
  cwd="$(
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null \
      | sed -n 's/^n//p' \
      | head -n 1
  )"
  [ -n "$cwd" ] || return 1
  cwd_canonical="$(cd "$cwd" 2>/dev/null && pwd -P || printf '%s\n' "$cwd")"
  [ "$cwd_canonical" = "$PLUGIN_ROOT_CANONICAL" ]
}

backend_pid_is_legacy_same_root_shared() {
  pid="$1"
  shared="$(shared_backend_pid 2>/dev/null || true)"
  [ -n "$shared" ] && [ "$pid" = "$shared" ] || return 1
  pid_cwd_matches_current_plugin_root "$pid"
}

backend_start_grace_seconds() {
  value="${CLAUDE_SMART_BACKEND_START_GRACE_SECONDS:-60}"
  case "$value" in
    ''|*[!0-9]*) printf '%s\n' "60" ;;
    *) printf '%s\n' "$value" ;;
  esac
}

recorded_backend_pid_in_startup_grace() {
  recorded_backend_pid_alive || return 1
  now="$(claude_smart_now_epoch 2>/dev/null || printf '%s\n' "0")"
  mtime="$(claude_smart_path_mtime_epoch "$PID_FILE" 2>/dev/null || printf '%s\n' "$now")"
  age=$((now - mtime))
  [ "$age" -lt "$(backend_start_grace_seconds)" ]
}

kill_pid_if_directionally_owned() {
  pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  is_claude_smart_backend_pid "$pid" || return 1
  if pid_uses_current_vendor_root "$pid" \
    || backend_pid_strictly_older_than_plugin "$pid" \
    || backend_pid_is_own_recorded "$pid" \
    || backend_pid_is_legacy_same_root_shared "$pid"; then
    kill_group "$pid"
    return 0
  fi
  return 1
}

kill_recorded_backend() {
  pid="$(recorded_backend_pid 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    if ! kill_pid_if_directionally_owned "$pid"; then
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
        "[claude-smart] backend: recorded pid $pid is not owned by this plugin; removing stale root-specific pid file"
    fi
    rm -f "$PID_FILE"
  fi
  shared_pid="$(shared_backend_pid 2>/dev/null || true)"
  if [ -n "$shared_pid" ]; then
    if kill_pid_if_directionally_owned "$shared_pid"; then
      rm -f "$SHARED_PID_FILE"
    else
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
        "[claude-smart] backend: shared pid $shared_pid is not owned by this plugin; leaving shared pid file"
    fi
  fi
  return 0
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
  kill_recorded_backend
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
    if ! claude_smart_service_lock "backend"; then
      if wait_for_health backend_current_or_compatible 3 1; then
        embedding_current_or_compatible || log_embedding_degraded
      fi
      emit_ok; exit 0
    fi
    trap 'claude_smart_service_unlock "backend"' EXIT
    listener_class="$(classify_backend_listener)"
    case "$listener_class" in
      claude_smart)
        if backend_current_or_compatible; then
          if ! backend_owned_by_current_vendor; then
            claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
              "[claude-smart] backend: accepted compatible claude-smart backend on port $PORT from another plugin root; not restarting"
          fi
          embedding_current_or_compatible || log_embedding_degraded
          emit_ok; exit 0
        fi
        ;;
      foreign)
        holder="$(port_holder "$PORT" 2>/dev/null || true)"
        msg="[claude-smart] backend: port $PORT held by another process${holder:+ ($holder)}; skipping start"
        claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" "$msg"
        echo "$msg" >&2
        echo "Free port $PORT (or stop the process above) and run /claude-smart:restart again." >&2
        emit_ok; exit 0
        ;;
    esac
    if is_our_backend_running; then
      embedding_current_or_compatible || log_embedding_degraded
      emit_ok; exit 0
    fi
    if recorded_backend_pid_in_startup_grace; then
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
        "[claude-smart] backend: recorded backend process is still running but not healthy yet; skipping duplicate start"
      emit_ok; exit 0
    fi
    if recorded_backend_pid_alive; then
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
        "[claude-smart] backend: recorded backend process exceeded startup grace without health; restarting"
      kill_recorded_backend
      sleep 1
    fi
    if stop_stale_backend_listener_if_owned; then
      claude_smart_append_capped_log "$LOG_FILE" "$LOG_MAX_BYTES" \
        "[claude-smart] backend: existing claude-smart backend is older than plugin $PLUGIN_VERSION; restarting local services"
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
    set -- services start --only backend --no-reload --workers "$workers"
    claude_smart_spawn_detached bash "$HERE/backend-log-runner.sh" \
      "$LOG_FILE" "$LOG_MAX_BYTES" -- \
      env PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}" \
      PYTHONPATH="$backend_pythonpath" \
      CLAUDE_SMART_BACKEND=1 \
      CLAUDE_SMART_PLUGIN_ROOT="$PLUGIN_ROOT_CANONICAL" \
      CLAUDE_SMART_VERSION="$PLUGIN_VERSION" \
      CLAUDE_SMART_REFLEXIO_VENDOR_ROOT="$VENDORED_REFLEXIO_FOR_PYTHON" \
      "$backend_python" "$HERE/backend-python-runner.py" \
      --claude-smart-backend=1 \
      "--claude-smart-plugin-root=$PLUGIN_ROOT_CANONICAL" \
      "--claude-smart-version=$PLUGIN_VERSION" \
      "--claude-smart-reflexio-vendor-root=$VENDORED_REFLEXIO_FOR_PYTHON" \
      -- "$@"
    svc_pid=$!
    echo "$svc_pid" > "$PID_FILE"

    # Give uvicorn about 20s to answer /health. The first boot after a fresh
    # checkout may be slower; if it catches up later, the next hook observes it.
    if wait_for_health backend_owned_by_current_vendor 10 1; then
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
    elif backend_current_or_compatible; then
      if embedding_current_or_compatible; then
        echo "running on http://localhost:$PORT (compatible claude-smart backend)"
      else
        echo "running on http://localhost:$PORT (compatible claude-smart backend; embedding degraded on http://127.0.0.1:$EMBEDDING_PORT)"
      fi
    elif backend_owned_by_stale_claude_smart; then
      echo "stale claude-smart backend on http://localhost:$PORT (older than plugin $PLUGIN_VERSION)"
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
