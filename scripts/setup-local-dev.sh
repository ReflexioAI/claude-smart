#!/usr/bin/env bash
# One-shot local-dev setup for claude-smart.
#
# Does what the published install wrapper does for end users, plus the
# extra steps that only make sense when you're iterating on this repo:
#   1. Install a side-by-side Reflexio checkout into the plugin venv as editable.
#   2. `uv sync` from plugin/.
#   3. Append CLAUDE_SMART_USE_LOCAL_CLI=1 / _USE_LOCAL_EMBEDDING=1 to
#      ~/.reflexio/.env so Reflexio runs without any external API key.
#   4. For Claude Code (default): prepare and register the local marketplace
#      (user scope) so `claude-smart@reflexioai-local` is available everywhere.
#   5. For Claude Code: disable the published claude-smart plugin at user scope
#      so Desktop/other projects don't load both plugins side by side.
#   6. For Claude Code: wire this repo's .claude/settings.local.json to enable
#      the local plugin and shadow the remote one for this project.
#   7. With `--read-only`, write CLAUDE_SMART_READ_ONLY=1 so local hooks
#      buffer but do not publish interactions to Reflexio.
#   8. For Codex (`--host codex` or `--host both`): run the maintained
#      Node install wrapper so Codex hooks are patched through the JSON-safe
#      codex-hook.js adapter, then install the same editable Reflexio checkout
#      into the Codex plugin cache venv used by those hooks.
#   9. Print a Reflexio source diagnostic so local/PyPI mixups are visible.
#
# Idempotent: safe to re-run.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PLUGIN_ROOT="$REPO_ROOT/plugin"

log() { printf '[setup-local-dev] %s\n' "$*" >&2; }

is_reflexio_checkout() {
  [ -f "$1/pyproject.toml" ] && [ -d "$1/reflexio" ]
}

expand_user_path() {
  case "$1" in
    "~")
      printf '%s\n' "$HOME"
      ;;
    "~/"*)
      printf '%s/%s\n' "$HOME" "${1#"~/"}"
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

resolve_venv_python() {
  venv_root="$1/.venv"
  if [ -x "$venv_root/bin/python" ]; then
    printf '%s\n' "$venv_root/bin/python"
    return 0
  fi
  if [ -x "$venv_root/Scripts/python.exe" ]; then
    printf '%s\n' "$venv_root/Scripts/python.exe"
    return 0
  fi
  return 1
}

resolve_reflexio_source() {
  if [ -n "${CLAUDE_SMART_LOCAL_REFLEXIO_PATH:-}" ]; then
    reflexio_env_path="$(expand_user_path "$CLAUDE_SMART_LOCAL_REFLEXIO_PATH")"
    if is_reflexio_checkout "$reflexio_env_path"; then
      (cd "$reflexio_env_path" && pwd)
      return 0
    fi
    log "ERROR: CLAUDE_SMART_LOCAL_REFLEXIO_PATH is not a Reflexio checkout: $reflexio_env_path"
    exit 1
  fi

  if [ -n "${REFLEXIO_PATH:-}" ]; then
    reflexio_env_path="$(expand_user_path "$REFLEXIO_PATH")"
    if is_reflexio_checkout "$reflexio_env_path"; then
      (cd "$reflexio_env_path" && pwd)
      return 0
    fi
    log "ERROR: REFLEXIO_PATH is not a Reflexio checkout: $reflexio_env_path"
    exit 1
  fi

  # In the standard development layout, claude-smart and reflexio are sibling
  # repositories.
  sibling_reflexio="$REPO_ROOT/../reflexio"
  if is_reflexio_checkout "$sibling_reflexio"; then
    (cd "$sibling_reflexio" && pwd)
    return 0
  fi

  log "ERROR: could not find a Reflexio checkout at $sibling_reflexio"
  log "Set REFLEXIO_PATH=/path/to/reflexio or CLAUDE_SMART_LOCAL_REFLEXIO_PATH=/path/to/reflexio."
  exit 1
}

verify_reflexio_client() {
  (cd "$PLUGIN_ROOT" && REFLEXIO_SOURCE_PATH="$REFLEXIO_ABS" uv run --no-sync --quiet python - <<'PY'
import inspect
import os
import sys

from reflexio import ReflexioClient

signature = inspect.signature(ReflexioClient.publish_interaction)
if "override_learning_stall" not in signature.parameters:
    print(
        "[setup-local-dev] ERROR: selected Reflexio client does not support "
        "override_learning_stall: "
        f"{os.environ.get('REFLEXIO_SOURCE_PATH')}",
        file=sys.stderr,
    )
    print(f"[setup-local-dev] signature: {signature}", file=sys.stderr)
    sys.exit(1)
PY
  )
}

codex_plugin_cache_root() {
  python3 - "$REPO_ROOT/plugin/.codex-plugin/plugin.json" "$HOME/.codex/plugins/cache/reflexioai/claude-smart" <<'PY'
import json
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
cache_root = Path(sys.argv[2])
version = json.loads(manifest.read_text())["version"]
print(cache_root / version)
PY
}

write_local_reflexio_uv_source() {
  project_root="$1"
  pyproject="$project_root/pyproject.toml"
  if [ ! -f "$pyproject" ]; then
    log "ERROR: expected pyproject.toml at $pyproject"
    exit 1
  fi
  python3 - "$pyproject" "$REFLEXIO_ABS" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
reflexio_path = sys.argv[2]
entry = f"reflexio-ai = {{ path = {json.dumps(reflexio_path)}, editable = true }}"
lines = path.read_text().splitlines()
out: list[str] = []
in_sources = False
sources_seen = False
entry_written = False

for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_sources and not entry_written:
            out.append(entry)
            entry_written = True
        in_sources = stripped == "[tool.uv.sources]"
        sources_seen = sources_seen or in_sources
        out.append(line)
        continue
    if in_sources and stripped.startswith("reflexio-ai "):
        continue
    out.append(line)

if in_sources and not entry_written:
    out.append(entry)
    entry_written = True

if not sources_seen:
    if out and out[-1] != "":
        out.append("")
    out.append("[tool.uv.sources]")
    out.append(entry)

path.write_text("\n".join(out) + "\n")
PY
}

install_editable_reflexio_into_codex_cache() {
  cache_root="$(codex_plugin_cache_root)"
  if [ ! -d "$cache_root" ]; then
    log "ERROR: Codex plugin cache was not created at $cache_root"
    exit 1
  fi

  marketplace_plugin_root="$HOME/.claude/plugins/marketplaces/reflexioai/plugin"
  log "writing local Reflexio source override into Codex marketplace snapshot..."
  write_local_reflexio_uv_source "$marketplace_plugin_root"
  write_local_reflexio_uv_source "$cache_root"

  log "installing editable Reflexio into Codex plugin cache..."
  uv sync --project "$cache_root"
  if ! cache_python="$(resolve_venv_python "$cache_root")"; then
    log "ERROR: Codex plugin cache Python was not created by uv sync under $cache_root/.venv"
    exit 1
  fi
  uv pip install --project "$cache_root" --python "$cache_python" -e "$REFLEXIO_ABS"
  REFLEXIO_SOURCE_PATH="$REFLEXIO_ABS" "$cache_python" - <<'PY'
import os
import sys
from pathlib import Path

import reflexio

expected_package = Path(os.environ["REFLEXIO_SOURCE_PATH"]).resolve() / "reflexio"
import_file = Path(reflexio.__file__).resolve()
try:
    import_file.relative_to(expected_package)
except ValueError:
    print(
        "[setup-local-dev] ERROR: Codex plugin cache imports Reflexio from "
        f"{import_file}, expected under {expected_package}",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"[setup-local-dev] Codex cache Reflexio import path: {import_file}")
PY
}

refresh_local_dashboard() {
  if [ ! -x "$PLUGIN_ROOT/scripts/dashboard-service.sh" ]; then
    return 0
  fi
  log "refreshing local dashboard process..."
  # The service script is marker-gated: it only kills a listener that answers
  # as claude-smart, so a foreign app on 3001 is left alone.
  bash "$PLUGIN_ROOT/scripts/dashboard-service.sh" stop >/dev/null 2>&1 || true
  bash "$PLUGIN_ROOT/scripts/dashboard-service.sh" start >/dev/null 2>&1 || true
}

force_plugin_root_to_checkout() {
  # Force-point ~/.reflexio/plugin-root at this repo's plugin/ so slash
  # commands resolve to editable in-repo sources (not a marketplace cache).
  # SessionStart's non-force self-heal will respect this link.
  if ! bash "$PLUGIN_ROOT/scripts/ensure-plugin-root.sh" "$PLUGIN_ROOT" --force; then
    log "WARNING: ensure-plugin-root failed; rerun manually:"
    log "  bash $PLUGIN_ROOT/scripts/ensure-plugin-root.sh $PLUGIN_ROOT --force"
  fi
}

usage() {
  cat >&2 <<'EOF'
Usage: scripts/setup-local-dev.sh [--host claude-code|codex|both] [--read-only]

Hosts:
  claude-code  Prepare local Claude Code marketplace/settings (default)
  codex        Install and trust the local plugin for Codex
  both         Do both host setups

Options:
  --read-only  Disable publish interactions hooks via ~/.reflexio/.env
EOF
}

HOST="claude-code"
READ_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host)
      if [ "$#" -lt 2 ]; then
        log "ERROR: --host requires claude-code, codex, or both."
        usage
        exit 2
      fi
      HOST="$2"
      shift 2
      ;;
    --host=*)
      HOST="${1#--host=}"
      shift
      ;;
    --read-only)
      READ_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    claude-code|codex|both)
      HOST="$1"
      shift
      ;;
    *)
      log "ERROR: unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

case "$HOST" in
  claude-code)
    SETUP_CLAUDE_CODE=1
    SETUP_CODEX=0
    ;;
  codex)
    SETUP_CLAUDE_CODE=0
    SETUP_CODEX=1
    ;;
  both)
    SETUP_CLAUDE_CODE=1
    SETUP_CODEX=1
    ;;
  *)
    log "ERROR: unsupported host '$HOST'; expected claude-code, codex, or both."
    usage
    exit 2
    ;;
esac

REFLEXIO_ABS="$(resolve_reflexio_source)"
log "using Reflexio source at $REFLEXIO_ABS"

if ! command -v uv >/dev/null 2>&1; then
  log "ERROR: uv not found — install it from https://docs.astral.sh/uv/ first."
  exit 1
fi
log "installing editable Reflexio into plugin environment..."
REFLEXIO_PATH="$REFLEXIO_ABS" bash "$REPO_ROOT/scripts/use-local-reflexio.sh"
verify_reflexio_client

REFLEXIO_ENV="$HOME/.reflexio/.env"
mkdir -p "$(dirname "$REFLEXIO_ENV")"
touch "$REFLEXIO_ENV"
if ! grep -q '^CLAUDE_SMART_USE_LOCAL_CLI=' "$REFLEXIO_ENV"; then
  printf '\n# Route reflexio generation through the local host CLI\nCLAUDE_SMART_USE_LOCAL_CLI=1\n' >> "$REFLEXIO_ENV"
  log "appended CLAUDE_SMART_USE_LOCAL_CLI=1 to $REFLEXIO_ENV"
fi
if ! grep -q '^CLAUDE_SMART_USE_LOCAL_EMBEDDING=' "$REFLEXIO_ENV"; then
  printf '# Use Reflexio local embedding support — no API key for semantic search\nCLAUDE_SMART_USE_LOCAL_EMBEDDING=1\n' >> "$REFLEXIO_ENV"
  log "appended CLAUDE_SMART_USE_LOCAL_EMBEDDING=1 to $REFLEXIO_ENV"
fi
python3 - "$REFLEXIO_ENV" "$READ_ONLY" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = "1" if sys.argv[2] == "1" else "0"
key = "CLAUDE_SMART_READ_ONLY"
lines = path.read_text().splitlines() if path.exists() else []
out = []
seen = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
        out.append(f'{key}="{value}"')
        seen = True
    else:
        out.append(line)
if not seen:
    out.append(f'{key}="{value}"')
path.write_text("\n".join(out) + "\n")
path.chmod(0o600)
PY
if [ "$READ_ONLY" = "1" ]; then
  log "set CLAUDE_SMART_READ_ONLY=1 in $REFLEXIO_ENV"
else
  log "set CLAUDE_SMART_READ_ONLY=0 in $REFLEXIO_ENV"
fi

if [ "$SETUP_CLAUDE_CODE" = "1" ]; then
  # Prepare and register the local marketplace with Claude Code (user-scope).
  # local-marketplace/ is gitignored because it contains a symlink to this
  # checkout's plugin/ directory. Its manifest declares name=reflexioai-local —
  # distinct from the remote `reflexioai`, so both can coexist in Claude Code's
  # marketplace list.
  LOCAL_MKT_DIR="$REPO_ROOT/local-marketplace"
  log "preparing local Claude Code marketplace at $LOCAL_MKT_DIR..."
  mkdir -p "$LOCAL_MKT_DIR/.claude-plugin"
  python3 - "$REPO_ROOT/.claude-plugin/marketplace.json" "$LOCAL_MKT_DIR/.claude-plugin/marketplace.json" <<'PY'
import json
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
data = json.loads(src.read_text())
data["name"] = "reflexioai-local"
data["plugins"][0]["source"] = "./plugin"
dst.write_text(json.dumps(data, indent=2) + "\n")
PY
  rm -rf "$LOCAL_MKT_DIR/plugin"
  ln -sfn "$PLUGIN_ROOT" "$LOCAL_MKT_DIR/plugin"

  if command -v claude >/dev/null 2>&1; then
    log "refreshing local marketplace registration with Claude Code..."
    # Local-dev marketplaces are worktree-sensitive. Always replace the
    # registration so rerunning setup from one worktree cannot keep a stale
    # reflexioai-local source from another worktree.
    if remove_output="$(claude plugin marketplace remove reflexioai-local 2>&1)"; then
      log "  removed existing reflexioai-local marketplace"
    else
      log "  no existing reflexioai-local marketplace to replace"
      log "  remove output: $remove_output"
    fi
    if add_output="$(claude plugin marketplace add "$LOCAL_MKT_DIR" 2>&1)"; then
      log "  added reflexioai-local → $LOCAL_MKT_DIR"
    else
      log "ERROR: failed to register reflexioai-local marketplace"
      log "  $add_output"
      exit 1
    fi

    log "installing/updating claude-smart@reflexioai-local for Claude Code..."
    if update_output="$(claude plugin update -s user claude-smart@reflexioai-local 2>&1)"; then
      log "  updated claude-smart@reflexioai-local"
    else
      log "  update did not complete; trying first-time install"
      if install_output="$(claude plugin install -s user claude-smart@reflexioai-local 2>&1)"; then
        log "  installed claude-smart@reflexioai-local"
      else
        log "ERROR: failed to install/update claude-smart@reflexioai-local"
        log "  update output: $update_output"
        log "  install output: $install_output"
        exit 1
      fi
    fi

    # `claude plugin disable -s user claude-smart@reflexioai` can be ambiguous
    # when both local and published marketplaces provide a plugin named
    # `claude-smart`. Write the user-scope enable map directly so Desktop and
    # non-repo Claude Code sessions don't load both copies.
    USER_SETTINGS_FILE="$HOME/.claude/settings.json"
    python3 - "$USER_SETTINGS_FILE" <<'PY'
import json
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])
settings_path.parent.mkdir(parents=True, exist_ok=True)

data: dict = {}
if settings_path.is_file():
    try:
        data = json.loads(settings_path.read_text() or "{}")
    except json.JSONDecodeError:
        print(
            f"[setup-local-dev] ERROR: {settings_path} is not valid JSON; "
            "refusing to overwrite",
            file=sys.stderr,
        )
        sys.exit(1)

if not isinstance(data, dict):
    print(
        f"[setup-local-dev] ERROR: {settings_path} top-level is not a JSON object",
        file=sys.stderr,
    )
    sys.exit(1)

enabled = data.get("enabledPlugins")
if not isinstance(enabled, dict):
    data["enabledPlugins"] = {}
    enabled = data["enabledPlugins"]
enabled["claude-smart@reflexioai-local"] = True
enabled["claude-smart@reflexioai"] = False

settings_path.write_text(json.dumps(data, indent=2) + "\n")
PY
    log "wrote $USER_SETTINGS_FILE (user-scope local enabled, @reflexioai disabled)"
  else
    log "WARNING: 'claude' CLI not on PATH — skipping marketplace registration."
    log "  Run it later: claude plugin marketplace add $LOCAL_MKT_DIR"
    log "  Then install it: claude plugin install -s user claude-smart@reflexioai-local"
  fi

  # Project-scoped enable/disable: turn on the local plugin and shadow the
  # remote one for this repo so they don't stack. The marketplace itself is
  # already registered at user scope above.
  SETTINGS_DIR="$REPO_ROOT/.claude"
  SETTINGS_FILE="$SETTINGS_DIR/settings.local.json"
  mkdir -p "$SETTINGS_DIR"
  python3 - "$SETTINGS_FILE" <<'PY'
import json
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])

data: dict = {}
if settings_path.is_file():
    try:
        data = json.loads(settings_path.read_text() or "{}")
    except json.JSONDecodeError:
        print(
            f"[setup-local-dev] ERROR: {settings_path} is not valid JSON; "
            "refusing to overwrite",
            file=sys.stderr,
        )
        sys.exit(1)

if not isinstance(data, dict):
    print(
        f"[setup-local-dev] ERROR: {settings_path} top-level is not a JSON object",
        file=sys.stderr,
    )
    sys.exit(1)

enabled = data.get("enabledPlugins")
if not isinstance(enabled, dict):
    data["enabledPlugins"] = {}
    enabled = data["enabledPlugins"]
enabled["claude-smart@reflexioai-local"] = True
# Shadow the remote so both don't load side-by-side in this repo.
enabled["claude-smart@reflexioai"] = False

settings_path.write_text(json.dumps(data, indent=2) + "\n")
PY
  log "wrote $SETTINGS_FILE (claude-smart@reflexioai-local enabled, @reflexioai shadowed off)"
fi

if [ "$SETUP_CODEX" = "1" ]; then
  if ! command -v codex >/dev/null 2>&1; then
    log "ERROR: 'codex' CLI not on PATH — cannot install Codex plugin support."
    exit 1
  fi
  if ! command -v node >/dev/null 2>&1; then
    log "ERROR: 'node' not on PATH — Codex setup needs the Node installer to patch hook commands."
    log "  Install Node.js 20.9+ or run the published npx installer."
    exit 1
  fi
  log "installing local claude-smart plugin for Codex..."
  rm -f "$HOME/plugins/claude-smart"
  (cd "$REPO_ROOT" && node bin/claude-smart.js install --host codex)
  install_editable_reflexio_into_codex_cache
fi

# Point ~/.reflexio/plugin-root at this repo's plugin/ regardless of host.
# Codex install rewrites the link to its cache; Claude Code's SessionStart
# self-heal would otherwise leave a stale cache target in place. Forcing this
# unconditionally at the end keeps slash commands and CLI shims resolving to
# the live working tree.
log "pointing ~/.reflexio/plugin-root at editable checkout..."
force_plugin_root_to_checkout

refresh_local_dashboard

log ""
if [ "$SETUP_CLAUDE_CODE" = "1" ] && [ "$SETUP_CODEX" = "1" ]; then
  log "done. Restart Claude Code and fully restart Codex to pick up the local plugin/hooks."
elif [ "$SETUP_CODEX" = "1" ]; then
  log "done. Fully restart Codex to pick up the local plugin/hooks."
else
  log "done. Restart Claude Code to pick up the local plugin."
fi
log "  plugin venv → editable reflexio-ai from $REFLEXIO_ABS"
log "  verify source → make doctor-reflexio"
log "  ~/.reflexio/.env → local-CLI + local-embedding providers"
if [ "$SETUP_CLAUDE_CODE" = "1" ]; then
  log "  Claude Code user marketplaces → reflexioai-local ($LOCAL_MKT_DIR)"
  log "  .claude/settings.local.json → claude-smart@reflexioai-local"
fi
if [ "$SETUP_CODEX" = "1" ]; then
  log "  Codex marketplace/cache/config → claude-smart@reflexioai"
  log "  Codex cache venv → editable reflexio-ai from $REFLEXIO_ABS"
  log "  Codex hooks → patched through scripts/codex-hook.js"
fi
