#!/usr/bin/env bash
# Run once on plugin install. Pulls the reflexio submodule, syncs the
# Python env, and flips on the claude-code LiteLLM provider in reflexio's
# .env so extraction works with no external API key.
#
# On failure, writes the reason to ~/.claude-smart/install-failed so
# hook_entry.sh can short-circuit and surface a user-visible message
# instead of silently no-op'ing every session.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"
claude_smart_source_login_path
claude_smart_prepend_astral_bins
claude_smart_prepend_node_bins

PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

MARKER_DIR="$HOME/.claude-smart"
FAILURE_MARKER="$MARKER_DIR/install-failed"
SUCCESS_MARKER="$MARKER_DIR/install-complete"
INSTALL_LOCK="$MARKER_DIR/install.lock"
INSTALL_REAP_LOCK="$MARKER_DIR/install.lock.reap"
mkdir -p "$MARKER_DIR"

remove_stale_install_lock() {
  local expected current

  expected="$1"
  if ! mkdir "$INSTALL_REAP_LOCK" 2>/dev/null; then
    sleep 1
    return 0
  fi
  current="$(cat "$INSTALL_LOCK" 2>/dev/null || true)"
  if [ "$current" = "$expected" ]; then
    rm -f "$INSTALL_LOCK"
  fi
  rmdir "$INSTALL_REAP_LOCK" 2>/dev/null || true
}

acquire_install_lock() {
  local lock_pid

  if command -v flock >/dev/null 2>&1; then
    exec 9>"$INSTALL_LOCK"
    if ! flock 9; then
      echo "[claude-smart] install lock failed; continuing without serialization" >&2
      claude_smart_emit_continue
      exit 0
    fi
    return 0
  fi

  while ! ( set -C; printf '%s\n' "$$" > "$INSTALL_LOCK" ) 2>/dev/null; do
    lock_pid="$(cat "$INSTALL_LOCK" 2>/dev/null || true)"
    case "$lock_pid" in
      ''|*[!0-9]*)
        remove_stale_install_lock "$lock_pid"
        ;;
      *)
        if kill -0 "$lock_pid" 2>/dev/null; then
          sleep 1
        else
          remove_stale_install_lock "$lock_pid"
        fi
        ;;
    esac
  done
  trap '[ "$(cat "$INSTALL_LOCK" 2>/dev/null || true)" = "$$" ] && rm -f "$INSTALL_LOCK" || true' EXIT
}

# Serialize concurrent installer runs (SessionStart hook + slash-command
# self-heal can both invoke this script). Wait for the active installer
# rather than returning early, otherwise callers can re-check uv before
# the first install has finished and report a false missing-dependency error.
acquire_install_lock

rm -f "$FAILURE_MARKER"

write_failure() {
  local reason fp_hash
  reason="$1"
  fp_hash="$(claude_smart_install_fingerprint_hash "$PLUGIN_ROOT" "$HERE" 2>/dev/null || true)"
  {
    printf '%s\n' "$reason"
    if [ -n "$fp_hash" ]; then
      printf 'fingerprint=%s\n' "$fp_hash"
    fi
  } > "$FAILURE_MARKER"
  rm -f "$SUCCESS_MARKER"
  echo "[claude-smart] install failed: $reason" >&2
  claude_smart_emit_continue
  exit 0
}

install_fingerprint() {
  claude_smart_install_fingerprint "$PLUGIN_ROOT" "$HERE"
}

install_complete() {
  [ -f "$SUCCESS_MARKER" ] || return 1
  [ "$(cat "$SUCCESS_MARKER" 2>/dev/null || true)" = "$(install_fingerprint)" ] || return 1
  command -v uv >/dev/null 2>&1 || return 1
  [ -d "$PLUGIN_ROOT/.venv" ] || return 1
  [ -f "$HOME/.reflexio/.env" ] || return 1
  grep -q '^CLAUDE_SMART_USE_LOCAL_CLI=' "$HOME/.reflexio/.env" || return 1
  grep -q '^CLAUDE_SMART_USE_LOCAL_EMBEDDING=' "$HOME/.reflexio/.env" || return 1
  if [ -d "$PLUGIN_ROOT/dashboard" ]; then
    [ -d "$PLUGIN_ROOT/dashboard/.next" ] || [ -f "$MARKER_DIR/dashboard-build.pid" ] || [ -f "$(claude_smart_dashboard_unavailable_marker)" ] || return 1
  fi
  return 0
}

write_success_marker() {
  install_fingerprint > "$SUCCESS_MARKER"
}

preflight_supported_runtime_platform() {
  local os_name machine darwin_major
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  machine="$(uname -m 2>/dev/null || echo unknown)"
  case "$os_name" in
    Darwin*)
      if [ "$machine" != "arm64" ]; then
        write_failure "claude-smart currently supports Apple Silicon macOS 14+ only; Intel Mac is not supported because native ML wheels are unavailable."
      fi
      darwin_major="$(uname -r 2>/dev/null | awk -F. '{print $1}')"
      case "$darwin_major" in
        ''|*[!0-9]*)
          write_failure "claude-smart could not determine the macOS version; Apple Silicon macOS 14+ is required."
          ;;
      esac
      if [ "$darwin_major" -lt 23 ]; then
        write_failure "claude-smart currently supports macOS 14+ on Apple Silicon; macOS 13 and older are not supported because native ML wheels are unavailable."
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*)
      case "$machine" in
        x86_64|amd64) : ;;
        *)
          write_failure "claude-smart currently supports Windows x64 only; Windows ARM is not supported because native ML wheels are unavailable."
          ;;
      esac
      ;;
    Linux*)
      : # Existing Linux installs remain supported when package wheels are available.
      ;;
    *)
      write_failure "claude-smart currently supports Apple Silicon macOS 14+, Windows x64, and Linux for vanilla installs."
      ;;
  esac
}

install_private_node() {
  local NODE_MIN_MAJOR NODE_MIN_MINOR NODE_LTS_MAJOR
  local node_os archive_ext reason node_arch node_platform base_url node_root
  local tmp_dir shasums_path archive_name ext_re install_dir archive_path
  local expected_hash actual_hash archive_win dest_win candidate_path next_link

  NODE_MIN_MAJOR=20
  NODE_MIN_MINOR=9
  NODE_LTS_MAJOR="${CLAUDE_SMART_NODE_LTS_MAJOR:-22}"

  if claude_smart_node_satisfies "$NODE_MIN_MAJOR" "$NODE_MIN_MINOR" \
    && claude_smart_npm_available; then
    claude_smart_clear_dashboard_unavailable
    return 0
  fi

  case "$(uname -s 2>/dev/null)" in
    Darwin*) node_os="darwin"; archive_ext="tar.gz" ;;
    Linux*) node_os="linux"; archive_ext="tar.gz" ;;
    MINGW*|MSYS*|CYGWIN*)
      node_os="win"
      archive_ext="zip"
      if ! command -v powershell >/dev/null 2>&1; then
        reason="PowerShell is not on PATH, so claude-smart could not extract private Node.js on Windows."
        echo "[claude-smart] WARNING: $reason" >&2
        claude_smart_write_dashboard_unavailable "$reason"
        return 1
      fi
      ;;
    *)
      reason="unsupported OS for private Node.js install: $(uname -s 2>/dev/null || echo unknown)"
      echo "[claude-smart] WARNING: $reason" >&2
      claude_smart_write_dashboard_unavailable "$reason"
      return 1
      ;;
  esac

  case "$(uname -m 2>/dev/null)" in
    x86_64|amd64) node_arch="x64" ;;
    arm64|aarch64) node_arch="arm64" ;;
    *)
      reason="unsupported CPU for private Node.js install: $(uname -m 2>/dev/null || echo unknown)"
      echo "[claude-smart] WARNING: $reason" >&2
      claude_smart_write_dashboard_unavailable "$reason"
      return 1
      ;;
  esac

  node_platform="$node_os-$node_arch"
  base_url="${CLAUDE_SMART_NODE_BASE_URL:-https://nodejs.org/dist/latest-v${NODE_LTS_MAJOR}.x}"
  echo "[claude-smart] Node.js >=20.9 with npm not found — installing private Node.js from nodejs.org..." >&2
  node_root="$HOME/.claude-smart/node"
  mkdir -p "$node_root"
  tmp_dir=$(mktemp -d "$node_root/tmp.XXXXXX") || {
    reason="could not create temporary directory under $node_root"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  }
  shasums_path="$tmp_dir/SHASUMS256.txt"

  if ! claude_smart_download "$base_url/SHASUMS256.txt" "$shasums_path"; then
    rm -rf "$tmp_dir"
    reason="could not download Node.js checksums from $base_url/SHASUMS256.txt"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  ext_re=$(printf '%s' "$archive_ext" | sed 's/\./\\./g')
  archive_name=$(
    awk -v platform="$node_platform" -v ext="$ext_re" \
          '$2 ~ ("^node-v[0-9][^ ]*-" platform "\\." ext "$") { print $2; exit }' \
      "$shasums_path"
  ) || archive_name=""
  if [ -z "$archive_name" ]; then
    rm -rf "$tmp_dir"
    reason="could not resolve Node.js archive for $node_platform from $base_url"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  install_dir="$node_root/${archive_name%.$archive_ext}"
  archive_path="$tmp_dir/$archive_name"
  expected_hash=$(awk -v name="$archive_name" '$2 == name { print $1; exit }' "$shasums_path")
  if [ -z "$expected_hash" ]; then
    rm -rf "$tmp_dir"
    reason="Node.js checksums did not include $archive_name"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  if ! claude_smart_download "$base_url/$archive_name" "$archive_path"; then
    rm -rf "$tmp_dir"
    reason="Node.js download failed from $base_url/$archive_name"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  actual_hash=$(claude_smart_sha256_file "$archive_path" || true)
  if [ -z "$actual_hash" ] || [ "$actual_hash" != "$expected_hash" ]; then
    rm -rf "$tmp_dir"
    reason="Node.js checksum verification failed for $archive_name"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  rm -rf "$install_dir" "$node_root/current.next"
  if [ "$archive_ext" = "zip" ]; then
    archive_win="$archive_path"
    dest_win="$node_root"
    if command -v cygpath >/dev/null 2>&1; then
      archive_win=$(cygpath -w "$archive_path")
      dest_win=$(cygpath -w "$node_root")
    fi
    if ! ARCHIVE_PATH="$archive_win" DEST_DIR="$dest_win" powershell -NoProfile -ExecutionPolicy Bypass -Command \
      '$ProgressPreference="SilentlyContinue"; Expand-Archive -LiteralPath $env:ARCHIVE_PATH -DestinationPath $env:DEST_DIR -Force' >&2; then
      rm -rf "$tmp_dir" "$install_dir"
      reason="Node.js archive extraction failed for $archive_name"
      echo "[claude-smart] WARNING: $reason" >&2
      claude_smart_write_dashboard_unavailable "$reason"
      return 1
    fi
  elif ! tar -xzf "$archive_path" -C "$node_root"; then
    rm -rf "$tmp_dir" "$install_dir"
    reason="Node.js archive extraction failed for $archive_name"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  if [ ! -d "$install_dir" ]; then
    rm -rf "$tmp_dir"
    reason="Node.js archive extracted without expected directory $install_dir"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  if [ -x "$install_dir/bin/node" ]; then
    candidate_path="$install_dir/bin:$PATH"
  elif [ -x "$install_dir/node.exe" ] || [ -x "$install_dir/node" ]; then
    candidate_path="$install_dir:$PATH"
  else
    rm -rf "$tmp_dir"
    reason="Node.js archive extracted without a node executable"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  if claude_smart_node_satisfies "$NODE_MIN_MAJOR" "$NODE_MIN_MINOR" \
    && claude_smart_npm_available; then
    : # existing PATH unexpectedly became suitable; keep going
  elif PATH="$candidate_path" claude_smart_node_satisfies "$NODE_MIN_MAJOR" "$NODE_MIN_MINOR" \
    && PATH="$candidate_path" claude_smart_npm_available; then
    : # candidate install is suitable
  else
    rm -rf "$tmp_dir"
    reason="private Node.js install completed but node/npm are still not usable"
    echo "[claude-smart] WARNING: $reason" >&2
    claude_smart_write_dashboard_unavailable "$reason"
    return 1
  fi

  next_link="$node_root/current.next.$$"
  if ln -s "$install_dir" "$next_link" 2>/dev/null; then
    if mv -Tf "$next_link" "$node_root/current" 2>/dev/null; then
      :
    elif mv -hf "$next_link" "$node_root/current" 2>/dev/null; then
      :
    else
      rm -rf "$node_root/current"
      mv "$next_link" "$node_root/current"
    fi
  else
    rm -rf "$next_link" "$node_root/current"
    mv "$install_dir" "$node_root/current"
  fi

  rm -rf "$tmp_dir"
  claude_smart_prepend_node_bins
  if claude_smart_node_satisfies "$NODE_MIN_MAJOR" "$NODE_MIN_MINOR" \
    && claude_smart_npm_available; then
    claude_smart_clear_dashboard_unavailable
    echo "[claude-smart] installed private $(node -v) at $node_root/current" >&2
    return 0
  fi

  reason="private Node.js install completed but current node/npm are still not usable"
  echo "[claude-smart] WARNING: $reason" >&2
  claude_smart_write_dashboard_unavailable "$reason"
  return 1
}

if [ "${CLAUDE_SMART_INSTALL_PRIVATE_NODE_ONLY:-}" = "1" ]; then
  install_private_node
  exit $?
fi

preflight_supported_runtime_platform

if install_complete; then
  claude_smart_emit_continue
  exit 0
fi

# Dev-mode only: when running from a git checkout, pull the reflexio
# submodule so tests/benchmarks can use its sources. In install mode the
# plugin lives under ~/.claude/plugins/cache and reflexio-ai resolves
# from PyPI instead. The guard checks for both `.git` and `.gitmodules`
# at REPO_ROOT to distinguish a dev checkout from a marketplace cache
# (where REPO_ROOT has neither).
if [ -d "$REPO_ROOT/.git" ] && [ -f "$REPO_ROOT/.gitmodules" ]; then
  echo "[claude-smart] initializing reflexio submodule..." >&2
  if ! (cd "$REPO_ROOT" && git submodule update --init --recursive reflexio) >&2; then
    echo "[claude-smart] WARNING: git submodule update failed; continuing with PyPI reflexio-ai" >&2
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[claude-smart] uv not found — installing from astral.sh..." >&2
  # The astral.sh bash installer downloads a zip and unzips it. On
  # Windows-flavoured bash (Git Bash / MSYS) the bundled `unzip` corrupts
  # the Windows uv binary (bad CRC on the inflated uv.exe), leaving the
  # install half-finished. Use the official PowerShell installer
  # (install.ps1) on Windows, which writes uv.exe to ~/.local/bin
  # natively — same destination the bash installer targets on POSIX, so
  # claude_smart_prepend_astral_bins picks it up uniformly afterwards.
  if claude_smart_is_windows; then
    if ! command -v powershell >/dev/null 2>&1; then
      write_failure "uv install needs PowerShell on Windows but powershell is not on PATH — install uv manually from https://docs.astral.sh/uv/"
    fi
    if ! powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" >&2; then
      write_failure "uv install via PowerShell failed — install manually from https://docs.astral.sh/uv/"
    fi
  else
    UV_INSTALLER="$MARKER_DIR/uv-install.sh"
    if ! claude_smart_download https://astral.sh/uv/install.sh "$UV_INSTALLER"; then
      write_failure "uv installer download failed — install manually from https://docs.astral.sh/uv/"
    fi
    if ! sh "$UV_INSTALLER" >&2; then
      write_failure "uv install failed — install manually from https://docs.astral.sh/uv/"
    fi
  fi
  claude_smart_prepend_astral_bins
  if ! command -v uv >/dev/null 2>&1; then
    UV_FOUND=""
    for candidate in "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.exe" "$HOME/.cargo/bin/uv" "$HOME/bin/uv"; do
      if [ -x "$candidate" ]; then
        UV_FOUND="$candidate"
        break
      fi
    done
    if [ -n "$UV_FOUND" ]; then
      write_failure "uv installed at $UV_FOUND — add its parent directory to PATH in your shell rc"
    else
      write_failure "uv install reported success but binary not found — install manually from https://docs.astral.sh/uv/"
    fi
  fi
fi

cd "$PLUGIN_ROOT"
echo "[claude-smart] running uv sync..." >&2
if ! uv sync --locked --python 3.12 --quiet >&2; then
  write_failure "uv sync failed in $PLUGIN_ROOT — run 'uv sync --locked --python 3.12' there to diagnose"
fi

# Reflexio's CLI reads ~/.reflexio/.env (see reflexio/cli/env_loader.py);
# append our two opt-in flags there so `reflexio services start` picks
# them up regardless of which directory the user runs it from.
REFLEXIO_ENV="$HOME/.reflexio/.env"
mkdir -p "$(dirname "$REFLEXIO_ENV")"
touch "$REFLEXIO_ENV"
if ! grep -q '^CLAUDE_SMART_USE_LOCAL_CLI=' "$REFLEXIO_ENV"; then
  printf '\n# Route reflexio generation through the local Claude Code CLI\nCLAUDE_SMART_USE_LOCAL_CLI=1\n' >> "$REFLEXIO_ENV"
  echo "[claude-smart] appended CLAUDE_SMART_USE_LOCAL_CLI=1 to $REFLEXIO_ENV" >&2
fi
if ! grep -q '^CLAUDE_SMART_USE_LOCAL_EMBEDDING=' "$REFLEXIO_ENV"; then
  printf '# Use the in-process ONNX embedder (chromadb) — no API key for semantic search\nCLAUDE_SMART_USE_LOCAL_EMBEDDING=1\n' >> "$REFLEXIO_ENV"
  echo "[claude-smart] appended CLAUDE_SMART_USE_LOCAL_EMBEDDING=1 to $REFLEXIO_ENV" >&2
fi
# Migrate stale REFLEXIO_URL from reflexio's library default (8081) to the
# plugin backend port (8071). Matches the quoted and unquoted forms but
# requires paired quotes, so malformed or deliberately different values
# (e.g. a remote reflexio URL) are preserved.
if grep -qE '^REFLEXIO_URL=("http://localhost:8081/?"|http://localhost:8081/?)$' "$REFLEXIO_ENV"; then
  sed -i.bak -E \
    -e 's|^REFLEXIO_URL="http://localhost:8081(/?)"$|REFLEXIO_URL="http://localhost:8071\1"|' \
    -e 's|^REFLEXIO_URL=http://localhost:8081(/?)$|REFLEXIO_URL=http://localhost:8071\1|' \
    "$REFLEXIO_ENV"
  echo "[claude-smart] migrated REFLEXIO_URL 8081 → 8071 in $REFLEXIO_ENV (backup at $REFLEXIO_ENV.bak)" >&2
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "[claude-smart] WARNING: 'claude' CLI not on PATH — reflexio extractors will have no LLM until it's installed" >&2
fi

LEGACY_CS_CITE="$HOME/.claude-smart/bin/cs-cite"
if [ -e "$LEGACY_CS_CITE" ]; then
  rm -f "$LEGACY_CS_CITE"
  echo "[claude-smart] removed legacy cs-cite helper at $LEGACY_CS_CITE" >&2
fi

CLAUDE_SETTINGS="$HOME/.claude/settings.json"
if [ -f "$CLAUDE_SETTINGS" ] && command -v node >/dev/null 2>&1; then
  node - "$CLAUDE_SETTINGS" <<'JS' >&2 || true
const fs = require("fs");
const path = process.argv[2];
const entry = "Bash(cs-cite:*)";
let data;
try {
  data = JSON.parse(fs.readFileSync(path, "utf8") || "{}");
} catch {
  process.exit(0);
}
const allow = data?.permissions?.allow;
if (!Array.isArray(allow)) process.exit(0);
const next = allow.filter((item) => item !== entry);
if (next.length === allow.length) process.exit(0);
data.permissions.allow = next;
fs.writeFileSync(path, `${JSON.stringify(data, null, 2)}\n`);
console.error(`[claude-smart] removed legacy ${entry} permission from ${path}`);
JS
fi

# Spawn the dashboard build detached so install returns immediately and
# Claude Code's install-hook timeout never kills a half-finished
# `next build` (which would force the user into a manual /claude-smart:restart
# recovery). dashboard-service.sh will also re-spawn this on SessionStart
# if .next is still missing, and dashboard-open.sh detects the build-pid
# file to surface a "still building" message instead of a generic error.
DASHBOARD_DIR="$PLUGIN_ROOT/dashboard"
if [ -d "$DASHBOARD_DIR" ]; then
  install_private_node || true
fi
if [ -d "$DASHBOARD_DIR" ] && claude_smart_npm_available; then
  echo "[claude-smart] starting dashboard build in background (~1-2 min on first install)" >&2
  claude_smart_spawn_detached bash "$HERE/dashboard-build.sh" >/dev/null 2>&1
elif [ -d "$DASHBOARD_DIR" ]; then
  reason="npm is not on PATH after private Node.js bootstrap"
  echo "[claude-smart] WARNING: $reason" >&2
  [ -f "$(claude_smart_dashboard_unavailable_marker)" ] || claude_smart_write_dashboard_unavailable "$reason"
fi

# Point ~/.reflexio/plugin-root at this install so slash commands can
# reference one stable short path regardless of which marketplace
# (reflexioai or reflexioai-local) loaded us.
if ! bash "$HERE/ensure-plugin-root.sh" "$PLUGIN_ROOT"; then
  echo "[claude-smart] WARNING: failed to set ~/.reflexio/plugin-root symlink — slash commands may not resolve" >&2
fi

write_success_marker
echo "[claude-smart] install complete. Backend and dashboard auto-start on session start." >&2
claude_smart_emit_continue
