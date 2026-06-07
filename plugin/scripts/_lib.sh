# Shared helpers for claude-smart plugin scripts. Source, do not execute.

# Claude Code hooks run with a minimal non-interactive PATH that often omits
# nvm/asdf/brew shims where `npm`, `uv`, etc. live. Pull the user's login-shell
# PATH the same way claude-mem does so hook-spawned scripts find them without
# the user having to mutate their global PATH. Best-effort — failures silent.
claude_smart_source_login_path() {
  local _SHELL_PATH
  if [ -n "${SHELL:-}" ] && [ -x "$SHELL" ]; then
    if _SHELL_PATH="$("$SHELL" -lc 'printf %s "$PATH"' 2>/dev/null)"; then
      [ -n "$_SHELL_PATH" ] && export PATH="$_SHELL_PATH:$PATH"
    fi
  fi
}

# Prepend the astral.sh installer's default bin directories to PATH so a
# freshly-installed `uv` is reachable before the user re-sources their
# shell rc. Prepend (not append) so the just-installed binary wins over
# any stale copy earlier in PATH. Literals only — no subshell, so safe
# under `set -u`.
claude_smart_prepend_astral_bins() {
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
}

# Prepend claude-smart's private Node.js install, if present. The Setup
# hook installs Node here when the user does not already have a suitable
# node/npm on PATH, so later hook-spawned dashboard scripts can run without
# requiring nvm/brew/global npm to be visible from Claude Code's shell.
claude_smart_prepend_node_bins() {
  local _CS_NODE_ROOT
  _CS_NODE_ROOT="$HOME/.claude-smart/node/current"
  export PATH="$_CS_NODE_ROOT/bin:$_CS_NODE_ROOT:$PATH"
}

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
  local env_file line key value
  env_file="$HOME/.reflexio/.env"
  [ -f "$env_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    case "$line" in
      \#*) continue ;;
      export\ *) line="${line#export }" ;;
    esac
    key="${line%%=*}"
    [ "$key" != "$line" ] || continue
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$key" in
      # Managed Reflexio config: file always wins so a rotated key/url overrides
      # whatever a long-lived managed backend or dashboard process inherited.
      REFLEXIO_URL|REFLEXIO_API_KEY|REFLEXIO_USER_ID|CLAUDE_SMART_READ_ONLY)
        value="$(claude_smart_env_unquote "$value")"
        export "$key=$value"
        ;;
      # CLAUDE_SMART_* flags: respect anything the caller already exported so
      # per-session overrides (e.g., a manual test) take precedence over the file.
      CLAUDE_SMART_USE_LOCAL_CLI|CLAUDE_SMART_USE_LOCAL_EMBEDDING|CLAUDE_SMART_BACKEND_AUTOSTART|CLAUDE_SMART_DASHBOARD_AUTOSTART|CLAUDE_SMART_CLI_PATH|CLAUDE_SMART_CLI_TIMEOUT|CLAUDE_SMART_STATE_DIR|CLAUDE_SMART_ENABLE_OPTIMIZER)
        if [ -z "$(eval "printf '%s' \"\${$key:-}\"")" ]; then
          value="$(claude_smart_env_unquote "$value")"
          export "$key=$value"
        fi
        ;;
    esac
  done < "$env_file"
}

claude_smart_reflexio_url_is_remote() {
  local url
  url="${REFLEXIO_URL:-}"
  [ -n "$url" ] || return 1
  case "$url" in
    http://localhost|http://localhost/|http://localhost:*|http://127.0.0.1|http://127.0.0.1/|http://127.0.0.1:*|http://0.0.0.0|http://0.0.0.0/|http://0.0.0.0:*|http://\[::1\]|http://\[::1\]/|http://\[::1\]:*)
      return 1
      ;;
  esac
  return 0
}

claude_smart_is_internal_invocation_env() {
  if [ "${CLAUDE_SMART_INTERNAL:-}" = "1" ]; then
    return 0
  fi
  case "${CLAUDE_CODE_ENTRYPOINT:-}" in
    ""|"cli"|"claude-desktop") return 1 ;;
    *) return 0 ;;
  esac
}

claude_smart_emit_continue() {
  if [ "${CLAUDE_SMART_HOST:-claude-code}" = "codex" ]; then
    echo '{"continue":true}'
  else
    echo '{"continue":true,"suppressOutput":true}'
  fi
}

claude_smart_dashboard_unavailable_marker() {
  printf '%s\n' "$HOME/.claude-smart/dashboard-unavailable"
}

claude_smart_node_recovery_hint() {
  cat <<'EOF'
Recovery:
  1. Restart Claude Code to let claude-smart retry its private Node.js install.
  2. If the retry is blocked by your network or OS policy, install Node.js 20.9+ manually:
EOF
  if claude_smart_is_windows; then
    cat <<'EOF'
     - winget install OpenJS.NodeJS.LTS
     - or download the LTS installer from https://nodejs.org/
EOF
  elif [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
    cat <<'EOF'
     - brew install node
     - or download the LTS installer from https://nodejs.org/
EOF
  else
    cat <<'EOF'
     - use your distro package manager for nodejs/npm
     - or download the LTS archive from https://nodejs.org/
EOF
  fi
  cat <<'EOF'
  3. Run /claude-smart:restart, then /claude-smart:dashboard.
EOF
}

claude_smart_write_dashboard_unavailable() {
  local reason marker
  reason="$1"
  marker="$(claude_smart_dashboard_unavailable_marker)"
  mkdir -p "$(dirname "$marker")"
  {
    printf 'claude-smart dashboard is unavailable: %s\n\n' "$reason"
    printf 'The learning backend and hooks can still work; only the dashboard UI is affected.\n\n'
    printf 'Private Node.js location: %s\n\n' "$HOME/.claude-smart/node/current"
    claude_smart_node_recovery_hint
  } > "$marker"
}

claude_smart_clear_dashboard_unavailable() {
  rm -f "$(claude_smart_dashboard_unavailable_marker)"
}

# Return 0 (true) if running under a Windows-flavoured bash (Git Bash,
# MSYS, Cygwin). Used to gate POSIX-only primitives (setsid, process
# groups) and route around Windows-specific potholes (the python3 App
# Execution Alias stub at WindowsApps\python3.exe).
claude_smart_is_windows() {
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

# Print the absolute path of a working python interpreter, or nothing
# (and return non-zero) if none is usable. On Windows, `python3` is
# usually the Microsoft Store "App Execution Alias" stub at
# %LocalAppData%\Microsoft\WindowsApps\python3.exe — `command -v python3`
# returns truthy but invoking it just prints a "Python was not found"
# message and exits non-zero. We probe with `-V` to filter the stub out
# and prefer `python` (the real interpreter when one is installed).
claude_smart_resolve_python() {
  if claude_smart_is_windows; then
    for cand in python python3; do
      if command -v "$cand" >/dev/null 2>&1 && "$cand" -V >/dev/null 2>&1; then
        command -v "$cand"
        return 0
      fi
    done
    return 1
  fi
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -V >/dev/null 2>&1; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}

claude_smart_plugin_python() {
  local plugin_root
  plugin_root="$1"
  if claude_smart_is_windows; then
    printf '%s\n' "$plugin_root/.venv/Scripts/python.exe"
  else
    printf '%s\n' "$plugin_root/.venv/bin/python"
  fi
}

claude_smart_python_imports() {
  local plugin_root module python_bin
  plugin_root="$1"
  module="$2"
  python_bin="$(claude_smart_plugin_python "$plugin_root")"
  [ -x "$python_bin" ] || return 1
  "$python_bin" - "$module" <<'PY' >/dev/null 2>&1
import importlib
import sys

importlib.import_module(sys.argv[1])
PY
}

claude_smart_canonical_dir() {
  local dir
  dir="$1"
  [ -d "$dir" ] || return 1
  (cd "$dir" 2>/dev/null && pwd -P)
}

# True when $1 canonicalizes to a plugin directory that physically lives
# *inside* ~/.reflexio. claude-smart's real install never places the live
# plugin tree there: the only entry under ~/.reflexio is the `plugin-root`
# symlink, whose `pwd -P` resolves to a cache dir *outside* ~/.reflexio
# (~/.claude, ~/.codex, or the npm global). So any plugin root that resolves
# to a descendant of ~/.reflexio is a stray host-made copy — e.g. the Windows
# cwd-derived `CUsers...` directories from issue #65 — that we must not
# bootstrap a heavy .venv/node_modules into.
#
# Detection is structural (descendant-of-~/.reflexio + plugin markers) rather
# than name-based on purpose: the host mangles the cwd differently per drive
# and user dir (C:\Users -> CUsers..., D:\repos -> Drepos..., varying case),
# so matching a fixed prefix like `Cu*` misses most real copies.
claude_smart_is_reflexio_session_copy() {
  local plugin_root reflexio_root
  plugin_root="$(claude_smart_canonical_dir "$1" 2>/dev/null || true)"
  [ -n "$plugin_root" ] || return 1
  # Must look like a plugin root — not ~/.reflexio/data, /configs, or .env.
  [ -f "$plugin_root/pyproject.toml" ] || return 1
  [ -d "$plugin_root/scripts" ] || return 1
  reflexio_root="$(claude_smart_canonical_dir "$HOME/.reflexio" 2>/dev/null || true)"
  [ -n "$reflexio_root" ] || return 1
  # Descendant of ~/.reflexio. The trailing slash on the subject plus the
  # "/" before * pins the boundary so a sibling like ~/.reflexio-bak/plugin
  # does not match.
  case "$plugin_root/" in
    "$reflexio_root"/*) return 0 ;;
  esac
  return 1
}

claude_smart_stable_plugin_root_for_session_copy() {
  local current candidate current_real candidate_real glob
  current="$1"
  if ! claude_smart_is_reflexio_session_copy "$current"; then
    return 1
  fi
  current_real="$(claude_smart_canonical_dir "$current" 2>/dev/null || true)"

  for candidate in \
    "$HOME/.reflexio/plugin-root" \
    "$HOME/.claude/plugins/marketplaces/reflexioai/plugin" \
    "$HOME/.codex/plugins/cache/reflexioai/claude-smart/current"
  do
    [ -f "$candidate/pyproject.toml" ] || continue
    candidate_real="$(claude_smart_canonical_dir "$candidate" 2>/dev/null || true)"
    [ -n "$candidate_real" ] || continue
    [ "$candidate_real" != "$current_real" ] || continue
    if claude_smart_is_reflexio_session_copy "$candidate_real"; then
      continue
    fi
    printf '%s\n' "$candidate_real"
    return 0
  done

  for glob in "$HOME/.claude/plugins/cache/reflexioai/claude-smart"/* "$HOME/.codex/plugins/cache/reflexioai/claude-smart"/*; do
    [ -f "$glob/pyproject.toml" ] || continue
    candidate_real="$(claude_smart_canonical_dir "$glob" 2>/dev/null || true)"
    [ -n "$candidate_real" ] || continue
    [ "$candidate_real" != "$current_real" ] || continue
    if claude_smart_is_reflexio_session_copy "$candidate_real"; then
      continue
    fi
    printf '%s\n' "$candidate_real"
    return 0
  done
  return 1
}

claude_smart_reexec_stable_plugin_root_if_needed() {
  local plugin_root script stable
  plugin_root="$1"
  script="$2"
  stable="$(claude_smart_stable_plugin_root_for_session_copy "$plugin_root" 2>/dev/null || true)"
  [ -n "$stable" ] || return 0
  # `-f` not `-x`: we re-run via `exec bash <script>`, which does not need the
  # executable bit. Cached copies on Windows/NTFS often lack +x, so requiring
  # -x would no-op the guard on the very platform issue #65 affects.
  [ -f "$stable/scripts/$script" ] || return 0
  echo "[claude-smart] redirecting stray plugin copy under ~/.reflexio ($plugin_root) to stable root $stable" >&2
  shift 2
  exec bash "$stable/scripts/$script" "$@"
}

claude_smart_download() {
  local url dest src _CS_PY
  url="$1"
  dest="$2"
  mkdir -p "$(dirname "$dest")"
  case "$url" in
    file://*)
      src="${url#file://}"
      cp "$src" "$dest"
      return $?
      ;;
  esac
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -O "$dest" "$url"
    return $?
  fi
  if command -v powershell >/dev/null 2>&1; then
    URL="$url" DEST="$dest" powershell -NoProfile -ExecutionPolicy Bypass -Command \
      '$ProgressPreference="SilentlyContinue"; Invoke-WebRequest -Uri $env:URL -OutFile $env:DEST'
    return $?
  fi
  if command -v pwsh >/dev/null 2>&1; then
    URL="$url" DEST="$dest" pwsh -NoProfile -Command \
      '$ProgressPreference="SilentlyContinue"; Invoke-WebRequest -Uri $env:URL -OutFile $env:DEST'
    return $?
  fi
  _CS_PY=$(claude_smart_resolve_python || true)
  if [ -n "$_CS_PY" ]; then
    "$_CS_PY" - "$url" "$dest" <<'PY'
import sys
import urllib.request

url, dest = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(url, timeout=60) as response:
    data = response.read()
with open(dest, "wb") as fh:
    fh.write(data)
PY
    return $?
  fi
  return 1
}

claude_smart_sha256_file() {
  local path _CS_PY
  path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return ${PIPESTATUS[0]}
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
    return ${PIPESTATUS[0]}
  fi
  if command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$path" | awk '{print $NF}'
    return ${PIPESTATUS[0]}
  fi
  _CS_PY=$(claude_smart_resolve_python || true)
  if [ -n "$_CS_PY" ]; then
    "$_CS_PY" - "$path" <<'PY'
import hashlib
import sys

h = hashlib.sha256()
with open(sys.argv[1], "rb") as fh:
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
    return $?
  fi
  return 1
}

# Print a short fingerprint line for a file ("<cksum>:<size>" or "missing").
claude_smart_fingerprint_file() {
  local path
  path="$1"
  if [ -f "$path" ]; then
    cksum "$path" 2>/dev/null | awk '{print $1 ":" $2}'
  else
    printf 'missing\n'
  fi
}

# Print a multi-line fingerprint covering every input that determines
# whether smart-install.sh can succeed. Order is stable so the hash is
# reproducible. Args: $1 = plugin_root, $2 = scripts_dir.
claude_smart_install_fingerprint() {
  local plugin_root script_dir
  plugin_root="$1"
  script_dir="$2"
  printf 'plugin_root=%s\n' "$plugin_root"
  printf 'smart_install=%s\n' "$(claude_smart_fingerprint_file "$script_dir/smart-install.sh")"
  printf 'lib=%s\n' "$(claude_smart_fingerprint_file "$script_dir/_lib.sh")"
  printf 'pyproject=%s\n' "$(claude_smart_fingerprint_file "$plugin_root/pyproject.toml")"
  printf 'uv_lock=%s\n' "$(claude_smart_fingerprint_file "$plugin_root/uv.lock")"
  printf 'vendor_reflexio_pyproject=%s\n' \
    "$(claude_smart_fingerprint_file "$plugin_root/vendor/reflexio/pyproject.toml")"
  # Resolved python interpreter — catches a system upgrade (3.12.4 → 3.12.5)
  # that would otherwise let install_complete return true against a venv
  # built against a now-deleted interpreter.
  if command -v uv >/dev/null 2>&1; then
    printf 'python=%s\n' "$(uv python find 3.12 2>/dev/null || echo missing)"
  else
    printf 'python=no-uv\n'
  fi
  if [ -d "$plugin_root/dashboard" ]; then
    printf 'dashboard_pkg=%s\n' "$(claude_smart_fingerprint_file "$plugin_root/dashboard/package.json")"
    printf 'dashboard_lock=%s\n' "$(claude_smart_fingerprint_file "$plugin_root/dashboard/package-lock.json")"
  else
    printf 'dashboard_pkg=none\n'
    printf 'dashboard_lock=none\n'
  fi
}

# Hash the install fingerprint to a single short hex string. Used by the
# install-failed marker so hook_entry.sh can detect when inputs change and
# auto-clear a stale marker. Args: $1 = plugin_root, $2 = scripts_dir.
claude_smart_install_fingerprint_hash() {
  local plugin_root script_dir tmp rc
  plugin_root="$1"
  script_dir="$2"
  tmp=$(mktemp 2>/dev/null) || return 1
  claude_smart_install_fingerprint "$plugin_root" "$script_dir" > "$tmp"
  claude_smart_sha256_file "$tmp"
  rc=$?
  rm -f "$tmp"
  return $rc
}

# Return 0 if `node` exists and satisfies the minimum major/minor pair.
# Patch versions are intentionally ignored because our requirement is a
# floor, not an exact runtime pin.
claude_smart_node_satisfies() {
  local min_major min_minor version major minor
  min_major="$1"
  min_minor="$2"
  command -v node >/dev/null 2>&1 || return 1
  version=$(node -v 2>/dev/null | sed 's/^v//') || return 1
  major=$(printf '%s' "$version" | awk -F. '{print $1}')
  minor=$(printf '%s' "$version" | awk -F. '{print $2}')
  [ -n "$major" ] && [ -n "$minor" ] || return 1
  case "$major:$minor" in
    *[!0-9:]*|:*|*:) return 1 ;;
  esac
  [ "$major" -gt "$min_major" ] && return 0
  [ "$major" -eq "$min_major" ] && [ "$minor" -ge "$min_minor" ]
}

claude_smart_resolve_npm() {
  local cand
  for cand in npm npm.cmd; do
    if command -v "$cand" >/dev/null 2>&1; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}

claude_smart_npm_available() {
  local npm_bin
  npm_bin=$(claude_smart_resolve_npm || true)
  [ -n "$npm_bin" ] || return 1
  "$npm_bin" --version >/dev/null 2>&1
}

claude_smart_log_max_bytes() {
  printf '%s\n' "10000000"
}

claude_smart_trim_log_file() {
  local file max_bytes size tmp
  file="$1"
  max_bytes="${2:-$(claude_smart_log_max_bytes)}"
  case "$max_bytes" in
    ''|*[!0-9]*) return 0 ;;
  esac
  [ -f "$file" ] || return 0
  size=$(wc -c < "$file" 2>/dev/null | tr -d '[:space:]') || return 0
  case "$size" in
    ''|*[!0-9]*) return 0 ;;
  esac
  [ "$size" -le "$max_bytes" ] && return 0

  tmp="${file}.trim.$$"
  if tail -c "$max_bytes" "$file" > "$tmp" 2>/dev/null; then
    # Rewrite the existing path instead of replacing it, so a process with
    # this file already open in append mode keeps writing to the capped file.
    cat "$tmp" > "$file"
  fi
  rm -f "$tmp"
}

claude_smart_append_capped_log() {
  local file max_bytes
  file="$1"
  max_bytes="${2:-$(claude_smart_log_max_bytes)}"
  shift 2
  mkdir -p "$(dirname "$file")"
  claude_smart_trim_log_file "$file" "$max_bytes"
  printf '%s\n' "$*" >> "$file"
  claude_smart_trim_log_file "$file" "$max_bytes"
}

# Spawn a command fully detached from the current shell so a hook timeout
# (Claude Code's install/SessionStart budget) cannot kill it mid-flight.
# POSIX: setsid → python3 os.setsid → nohup (in that order of strength).
# Windows: nohup alone — Git Bash has no setsid, no process groups, and
# `os.setsid()` is POSIX-only; nohup ignores SIGHUP which is enough to
# survive the parent console closing. The python3 fallback is gated on a
# real-interpreter probe (-V) so the Windows App Execution Alias stub
# doesn't get invoked. Caller is responsible for redirecting stdout/stderr;
# we do not impose a log destination here. Stdin is closed so the child
# cannot inherit a tty. Use `$!` after this call to capture the pid.
claude_smart_spawn_detached() {
  if claude_smart_is_windows; then
    nohup "$@" < /dev/null &
    return 0
  fi
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$@" < /dev/null &
  elif _CS_PY=$(claude_smart_resolve_python) && [ -n "$_CS_PY" ]; then
    "$_CS_PY" -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      "$@" < /dev/null &
  else
    nohup "$@" < /dev/null &
  fi
}

# Terminate a process and (on POSIX) its whole process group, escalating
# from TERM to KILL after a short grace period. On Windows there are no
# POSIX process groups, so we use `taskkill /T /F /PID` which walks the
# child-process tree via the Windows job-object/parent-pid relationships
# — the closest equivalent to a group kill.
#
# Windows-specific subtlety: in Git Bash / MSYS, `$!` for a backgrounded
# job returns the MSYS pid (an internal counter), NOT the native Windows
# pid that taskkill needs. `ps -W` (or `-o winpid=`) exposes the WINPID
# column for the translation. If the lookup fails we fall back to
# treating the input as a native pid, so callers can pass either an MSYS
# pid (recorded via $!) or a Windows pid (from tasklist) interchangeably.
# The `//T //F //PID` syntax escapes Git Bash's MSYS path-mangling of
# arguments that begin with `/`.
claude_smart_kill_tree() {
  pid="$1"
  [ -z "$pid" ] && return 0
  if claude_smart_is_windows; then
    # Git Bash's `ps` is the procps fork, not BSD/Linux ps; it has no
    # -o option but its default header is `PID PPID PGID WINPID TTY ...`,
    # so column 4 of the data row is the Windows pid. awk extracts it
    # without depending on -o support.
    target=""
    if command -v ps >/dev/null 2>&1; then
      target=$(ps -p "$pid" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d ' \r\n' || true)
    fi
    [ -z "$target" ] && target="$pid"
    if command -v taskkill >/dev/null 2>&1; then
      taskkill //T //F //PID "$target" >/dev/null 2>&1 || true
    else
      kill -TERM "$pid" 2>/dev/null || true
      sleep 0.5
      kill -KILL "$pid" 2>/dev/null || true
    fi
    return 0
  fi
  current_pgid=""
  if command -v ps >/dev/null 2>&1; then
    current_pgid=$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ')
  fi
  if [ -n "$current_pgid" ] && [ "$pid" = "$current_pgid" ]; then
    return 0
  fi
  if ! kill -TERM -- "-$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.5
    kill -KILL "$pid" 2>/dev/null || true
    return 0
  fi
  for _ in 1 2 3 4 5; do
    kill -0 -- "-$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
}

# Return 0 (true) if $1 names a pid file whose pid is currently alive.
# Silent on missing/empty/stale files.
claude_smart_pid_alive_file() {
  pid_file="$1"
  [ -f "$pid_file" ] || return 1
  pid=$(cat "$pid_file" 2>/dev/null || echo "")
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}
