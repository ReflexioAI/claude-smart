# Frozen predecessor env reader:104bc46202e2a5087a558d038c3289a7bd984b0a
# Source:plugin/scripts/_lib.sh (unquote and source functions only).
# Body SHA256:74f99af59105ec004574596b03eec7e01e2d5b40ace54fed7cacd7a2866c5e47
claude_smart_env_unquote() {
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

claude_smart_source_reflexio_env() {
  # claude-smart's env lives at ~/.claude-smart/.env (kept separate from the OSS
  # reflexio default ~/.reflexio/.env). Stays in sync with
  # env_config.CLAUDE_SMART_ENV_PATH and the REFLEXIO_ENV_FILE export in
  # backend-service.sh.
  local env_file line key value
  env_file="$HOME/.claude-smart/.env"
  [ -f "$env_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    # UTF-8 BOM metadata must not become part of the first assignment's key.
    line="${line#$'\xEF\xBB\xBF'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    case "$line" in
      \#*) continue ;;
      export[[:space:]]*)
        line="${line#export}"
        line="${line#"${line%%[![:space:]]*}"}"
        ;;
    esac
    key="${line%%=*}"
    [ "$key" != "$line" ] || continue
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$key" in
      # Shared connection settings: file wins over values inherited
      # by a long-lived backend/dashboard or exported by a per-agent hook.
      REFLEXIO_URL|REFLEXIO_API_KEY|REFLEXIO_USER_ID|CLAUDE_SMART_READ_ONLY)
        value="$(claude_smart_env_unquote "$value")"
        export "$key=$value"
        ;;
      # CLAUDE_SMART_* flags: respect anything the caller already exported so
      # per-session overrides (e.g., a manual test) take precedence over the file.
      CLAUDE_SMART_HOST|CLAUDE_SMART_BACKEND_AUTOSTART|CLAUDE_SMART_DASHBOARD_AUTOSTART|CLAUDE_SMART_CLI_PATH|CLAUDE_SMART_OPENCODE_PATH|CLAUDE_SMART_CLI_TIMEOUT|CLAUDE_SMART_STATE_DIR)
        if [ -z "$(eval "printf '%s' \"\${$key:-}\"")" ]; then
          value="$(claude_smart_env_unquote "$value")"
          export "$key=$value"
        fi
        ;;
    esac
  done < "$env_file"
}
