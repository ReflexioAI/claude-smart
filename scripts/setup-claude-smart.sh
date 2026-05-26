#!/usr/bin/env bash
# Interactive setup for claude-smart's shared ~/.reflexio/.env.
#
# Plain installs stay local and do not need this script. Use this when a user
# wants managed Reflexio, read-only mode, global sharing, or to switch back to
# local mode after a managed setup.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
INSTALLER="$REPO_ROOT/bin/claude-smart.js"
REFLEXIO_ENV="${REFLEXIO_ENV_PATH:-$HOME/.reflexio/.env}"
MANAGED_REFLEXIO_URL="https://www.reflexio.ai/"
GLOBAL_USER_ID="global_user"

log() { printf '[claude-smart setup] %s\n' "$*" >&2; }

env_unquote() {
  local value first last
  value="$1"
  first="${value%"${value#?}"}"
  last="${value#"${value%?}"}"
  if { [ "$first" = '"' ] && [ "$last" = '"' ]; } || { [ "$first" = "'" ] && [ "$last" = "'" ]; }; then
    value="${value#?}"
    value="${value%?}"
  fi
  printf '%s\n' "$value"
}

env_quote() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

get_env_value() {
  local key line raw_key value
  key="$1"
  [ -f "$REFLEXIO_ENV" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    case "$line" in
      \#*) continue ;;
      export\ *) line="${line#export }" ;;
    esac
    raw_key="${line%%=*}"
    [ "$raw_key" != "$line" ] || continue
    raw_key="${raw_key%"${raw_key##*[![:space:]]}"}"
    [ "$raw_key" = "$key" ] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    env_unquote "$value"
    return 0
  done < "$REFLEXIO_ENV"
  return 1
}

remove_env_keys() {
  local tmp line key raw_key remove target
  [ -f "$REFLEXIO_ENV" ] || return 0
  tmp="$(mktemp "${REFLEXIO_ENV}.tmp.XXXXXX")"
  while IFS= read -r line || [ -n "$line" ]; do
    raw_key="$line"
    raw_key="${raw_key#"${raw_key%%[![:space:]]*}"}"
    case "$raw_key" in
      export\ *) raw_key="${raw_key#export }" ;;
    esac
    key="${raw_key%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    remove=0
    if [ "$key" != "$raw_key" ]; then
      if [ "$#" -gt 0 ]; then
        for target in "$@"; do
          [ "$key" = "$target" ] && remove=1
        done
      else
        case "$key" in
          REFLEXIO_API_KEY|REFLEXIO_URL|REFLEXIO_USER_ID|CLAUDE_SMART_READ_ONLY)
            remove=1
            ;;
        esac
      fi
    fi
    [ "$remove" = "1" ] || printf '%s\n' "$line" >> "$tmp"
  done < "$REFLEXIO_ENV"

  if ! grep -q '[^[:space:]]' "$tmp"; then
    rm -f "$tmp" "$REFLEXIO_ENV"
    return 0
  fi
  mv "$tmp" "$REFLEXIO_ENV"
  chmod 600 "$REFLEXIO_ENV"
}

append_env_value() {
  local key value quoted
  key="$1"
  value="$2"
  quoted="$(env_quote "$value")"
  mkdir -p "$(dirname "$REFLEXIO_ENV")"
  touch "$REFLEXIO_ENV"
  chmod 600 "$REFLEXIO_ENV"
  printf '%s="%s"\n' "$key" "$quoted" >> "$REFLEXIO_ENV"
  chmod 600 "$REFLEXIO_ENV"
}

append_env_raw() {
  local key value
  key="$1"
  value="$2"
  mkdir -p "$(dirname "$REFLEXIO_ENV")"
  touch "$REFLEXIO_ENV"
  chmod 600 "$REFLEXIO_ENV"
  printf '%s=%s\n' "$key" "$value" >> "$REFLEXIO_ENV"
  chmod 600 "$REFLEXIO_ENV"
}

ensure_local_env_defaults() {
  mkdir -p "$(dirname "$REFLEXIO_ENV")"
  touch "$REFLEXIO_ENV"
  chmod 600 "$REFLEXIO_ENV"
  if ! get_env_value CLAUDE_SMART_USE_LOCAL_CLI >/dev/null 2>&1; then
    printf '# Route reflexio generation through the local Claude Code CLI\n' >> "$REFLEXIO_ENV"
    append_env_raw CLAUDE_SMART_USE_LOCAL_CLI "1"
  fi
  if ! get_env_value CLAUDE_SMART_USE_LOCAL_EMBEDDING >/dev/null 2>&1; then
    printf '# Use the in-process ONNX embedder (chromadb) - no API key for semantic search\n' >> "$REFLEXIO_ENV"
    append_env_raw CLAUDE_SMART_USE_LOCAL_EMBEDDING "1"
  fi
  if ! get_env_value CLAUDE_SMART_READ_ONLY >/dev/null 2>&1; then
    append_env_value CLAUDE_SMART_READ_ONLY "0"
  fi
}

mask_secret() {
  local value len suffix
  value="$1"
  len="${#value}"
  if [ "$len" -eq 0 ]; then
    printf '%s\n' ''
  elif [ "$len" -le 4 ]; then
    printf '****%s\n' "$value"
  else
    suffix="${value: -4}"
    printf '****%s\n' "$suffix"
  fi
}

prompt() {
  local message default answer
  message="$1"
  default="$2"
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$message" "$default" >&2
  else
    printf '%s: ' "$message" >&2
  fi
  IFS= read -r answer
  if [ -z "$answer" ]; then
    answer="$default"
  fi
  printf '%s\n' "$answer"
}

prompt_api_key() {
  local existing masked answer
  existing="$1"
  masked="$(mask_secret "$existing")"
  while :; do
    if [ -n "$existing" ]; then
      printf 'Reflexio API key [%s; press Enter to keep]: ' "$masked" >&2
    else
      printf 'Reflexio API key: ' >&2
    fi
    if [ -t 0 ]; then
      IFS= read -rs answer
      printf '\n' >&2
    else
      IFS= read -r answer
    fi
    if [ -z "$answer" ] && [ -n "$existing" ]; then
      answer="$existing"
    fi
    if [ -z "$answer" ]; then
      log "API key is required for managed Reflexio."
      continue
    fi
    case "$answer" in
      *[[:space:]]*)
        log "API key must not contain whitespace."
        continue
        ;;
    esac
    printf '%s\n' "$answer"
    return 0
  done
}

normalize_host() {
  case "$1" in
    1|claude|claude-code|Claude*) printf 'claude-code\n' ;;
    2|codex|Codex*) printf 'codex\n' ;;
    3|both|Both*) printf 'both\n' ;;
    *) return 1 ;;
  esac
}

normalize_mode() {
  case "$1" in
    1|local|Local*) printf 'local\n' ;;
    2|managed|remote|Remote*) printf 'managed\n' ;;
    *) return 1 ;;
  esac
}

normalize_yes_no() {
  case "$1" in
    y|Y|yes|Yes|YES|true|1) printf 'yes\n' ;;
    n|N|no|No|NO|false|0) printf 'no\n' ;;
    *) return 1 ;;
  esac
}

normalize_scope() {
  case "$1" in
    1|project|Project*) printf 'project\n' ;;
    2|global|Global*) printf 'global\n' ;;
    *) return 1 ;;
  esac
}

is_local_url() {
  case "$1" in
    http://localhost|http://localhost/|http://localhost:*|http://127.0.0.1|http://127.0.0.1/|http://127.0.0.1:*|http://0.0.0.0|http://0.0.0.0/|http://0.0.0.0:*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

prompt_normalized() {
  local label default normalizer answer normalized
  label="$1"
  default="$2"
  normalizer="$3"
  while :; do
    answer="$(prompt "$label" "$default")"
    if normalized="$("$normalizer" "$answer")"; then
      printf '%s\n' "$normalized"
      return 0
    fi
    log "Invalid choice: $answer"
  done
}

install_for_host() {
  local host node_bin
  host="$1"
  if [ "${CLAUDE_SMART_SETUP_NO_INSTALL:-}" = "1" ]; then
    log "skipping plugin install because CLAUDE_SMART_SETUP_NO_INSTALL=1"
    return 0
  fi
  node_bin="${NODE:-node}"
  if [ ! -f "$INSTALLER" ]; then
    log "ERROR: installer not found at $INSTALLER"
    exit 1
  fi
  case "$host" in
    claude-code)
      "$node_bin" "$INSTALLER" install
      ;;
    codex)
      "$node_bin" "$INSTALLER" install --host codex
      ;;
    both)
      "$node_bin" "$INSTALLER" install
      "$node_bin" "$INSTALLER" install --host codex
      ;;
  esac
}

main() {
  local existing_api_key existing_url existing_user_id existing_read_only
  local default_mode default_read_only default_scope host mode api_key read_only scope
  local managed_url managed_user_id

  existing_api_key="$(get_env_value REFLEXIO_API_KEY || true)"
  existing_url="$(get_env_value REFLEXIO_URL || true)"
  existing_user_id="$(get_env_value REFLEXIO_USER_ID || true)"
  existing_read_only="$(get_env_value CLAUDE_SMART_READ_ONLY || true)"

  default_mode="local"
  if [ -n "$existing_api_key" ] || [ -n "$existing_url" ]; then
    default_mode="managed"
  fi
  default_read_only="no"
  case "$existing_read_only" in
    1|true|yes|on) default_read_only="yes" ;;
  esac
  default_scope="project"
  if [ -n "$existing_user_id" ]; then
    default_scope="global"
  fi

  host="$(prompt_normalized "Host (1=Claude Code, 2=Codex, 3=both)" "claude-code" normalize_host)"
  mode="$(prompt_normalized "Setup mode (1=local, 2=managed Reflexio)" "$default_mode" normalize_mode)"

  if [ "$mode" = "local" ]; then
    if [ -f "$REFLEXIO_ENV" ]; then
      remove_env_keys REFLEXIO_API_KEY REFLEXIO_URL REFLEXIO_USER_ID
      log "removed managed Reflexio settings from $REFLEXIO_ENV"
    fi
    ensure_local_env_defaults
    log "configured local Reflexio defaults in $REFLEXIO_ENV"
    install_for_host "$host"
    return 0
  fi

  api_key="$(prompt_api_key "$existing_api_key")"
  read_only="$(prompt_normalized "Read-only mode? (yes/no)" "$default_read_only" normalize_yes_no)"
  scope="$(prompt_normalized "Sharing scope (1=project, 2=global)" "$default_scope" normalize_scope)"

  managed_url="${existing_url:-$MANAGED_REFLEXIO_URL}"
  if is_local_url "$managed_url"; then
    managed_url="$MANAGED_REFLEXIO_URL"
  fi
  managed_user_id=""
  if [ "$scope" = "global" ]; then
    managed_user_id="${existing_user_id:-$GLOBAL_USER_ID}"
  fi

  remove_env_keys \
    REFLEXIO_API_KEY \
    REFLEXIO_URL \
    REFLEXIO_USER_ID \
    CLAUDE_SMART_READ_ONLY \
    CLAUDE_SMART_USE_LOCAL_CLI \
    CLAUDE_SMART_USE_LOCAL_EMBEDDING
  append_env_value REFLEXIO_URL "$managed_url"
  append_env_value REFLEXIO_API_KEY "$api_key"
  if [ "$read_only" = "yes" ]; then
    append_env_value CLAUDE_SMART_READ_ONLY "1"
  fi
  if [ -n "$managed_user_id" ]; then
    append_env_value REFLEXIO_USER_ID "$managed_user_id"
  fi
  chmod 600 "$REFLEXIO_ENV"
  log "configured managed Reflexio in $REFLEXIO_ENV"
  log "If a real API key was pasted visibly into a terminal or chat, rotate it in Reflexio."
  install_for_host "$host"
}

main "$@"
