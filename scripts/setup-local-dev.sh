#!/usr/bin/env bash
# One-shot local-dev setup for claude-smart.
#
# Does what the published install wrapper does for end users, plus the
# extra steps that only make sense when you're iterating on this repo:
#   1. Initialize the reflexio submodule.
#   2. Uncomment the `[tool.uv.sources]` block in plugin/pyproject.toml so
#      uv resolves reflexio-ai from the vendored submodule (editable).
#   3. `git update-index --skip-worktree` plugin/pyproject.toml + uv.lock
#      so the local divergence is invisible to `git status`.
#   4. `uv sync` from plugin/.
#   5. Append CLAUDE_SMART_USE_LOCAL_CLI=1 / _USE_LOCAL_EMBEDDING=1 to
#      ~/.reflexio/.env so reflexio runs without any external API key.
#   6. For Claude Code (default): prepare and register the local marketplace
#      (user scope) so `claude-smart@reflexioai-local` is available everywhere.
#   7. For Claude Code: wire this repo's .claude/settings.local.json to enable
#      the local plugin and shadow the remote one for this project.
#   8. For Codex (`--host codex` or `--host both`): run the maintained
#      Node install wrapper so Codex hooks are patched through the JSON-safe
#      codex-hook.js adapter.
#
# Idempotent: safe to re-run.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PLUGIN_ROOT="$REPO_ROOT/plugin"
PYPROJECT="$PLUGIN_ROOT/pyproject.toml"
LOCKFILE="$PLUGIN_ROOT/uv.lock"

log() { printf '[setup-local-dev] %s\n' "$*" >&2; }

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

usage() {
  cat >&2 <<'EOF'
Usage: scripts/setup-local-dev.sh [--host claude-code|codex|both]

Hosts:
  claude-code  Prepare local Claude Code marketplace/settings (default)
  codex        Install and trust the local plugin for Codex
  both         Do both host setups
EOF
}

HOST="claude-code"
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

log "initializing reflexio submodule..."
(cd "$REPO_ROOT" && git submodule update --init --recursive reflexio)

log "enabling [tool.uv.sources] override in plugin/pyproject.toml..."
# Use an absolute path for the reflexio submodule. Relative paths like
# "../reflexio" get resolved by uv against the literal --project path,
# which breaks when slash commands pass the ~/.reflexio/plugin-root
# symlink (uv does not canonicalize) — it would resolve to
# $HOME/.reflexio/reflexio, which does not exist.
REFLEXIO_ABS="$REPO_ROOT/reflexio"
sed -i.bak -E \
  -e 's|^# \[tool\.uv\.sources\]$|[tool.uv.sources]|' \
  -e "s|^(# )?reflexio-ai = \\{ path = \"[^\"]*\", editable = true \\}\$|reflexio-ai = { path = \"$REFLEXIO_ABS\", editable = true }|" \
  "$PYPROJECT"
rm -f "$PYPROJECT.bak"

# Hide local divergence in pyproject.toml + uv.lock from `git status`. See
# DEVELOPER.md ("Developing locally") for the rationale.
git -C "$REPO_ROOT" update-index --skip-worktree plugin/pyproject.toml plugin/uv.lock

if ! command -v uv >/dev/null 2>&1; then
  log "ERROR: uv not found — install it from https://docs.astral.sh/uv/ first."
  exit 1
fi
log "running uv sync in plugin/ ..."
(cd "$PLUGIN_ROOT" && uv sync --quiet)

REFLEXIO_ENV="$HOME/.reflexio/.env"
mkdir -p "$(dirname "$REFLEXIO_ENV")"
touch "$REFLEXIO_ENV"
if ! grep -q '^CLAUDE_SMART_USE_LOCAL_CLI=' "$REFLEXIO_ENV"; then
  printf '\n# Route reflexio generation through the local host CLI\nCLAUDE_SMART_USE_LOCAL_CLI=1\n' >> "$REFLEXIO_ENV"
  log "appended CLAUDE_SMART_USE_LOCAL_CLI=1 to $REFLEXIO_ENV"
fi
if ! grep -q '^CLAUDE_SMART_USE_LOCAL_EMBEDDING=' "$REFLEXIO_ENV"; then
  printf '# Use the in-process ONNX embedder (chromadb) — no API key for semantic search\nCLAUDE_SMART_USE_LOCAL_EMBEDDING=1\n' >> "$REFLEXIO_ENV"
  log "appended CLAUDE_SMART_USE_LOCAL_EMBEDDING=1 to $REFLEXIO_ENV"
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
    log "registering local marketplace with Claude Code..."
    marketplace_list="$(claude plugin marketplace list 2>/dev/null || true)"
    if printf '%s\n' "$marketplace_list" | grep -Fq "reflexioai-local" || printf '%s\n' "$marketplace_list" | grep -Fq "$LOCAL_MKT_DIR"; then
      log "  reflexioai-local already registered"
    else
      if add_output="$(claude plugin marketplace add "$LOCAL_MKT_DIR" 2>&1)"; then
        log "  added reflexioai-local → $LOCAL_MKT_DIR"
      else
        log "ERROR: failed to register reflexioai-local marketplace"
        log "  $add_output"
        exit 1
      fi
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

enabled = data.setdefault("enabledPlugins", {})
enabled["claude-smart@reflexioai-local"] = True
# Shadow the remote so both don't load side-by-side in this repo.
enabled["claude-smart@reflexioai"] = False

settings_path.write_text(json.dumps(data, indent=2) + "\n")
PY
  log "wrote $SETTINGS_FILE (claude-smart@reflexioai-local enabled, @reflexioai shadowed off)"

  # Force-point ~/.reflexio/plugin-root at this repo's plugin/ so slash
  # commands resolve to editable in-repo sources (not the marketplace
  # cache). SessionStart's non-force self-heal will respect this link.
  # Warn (don't abort) on failure — marketplace registration and settings
  # have already been committed by this point, so aborting would leave the
  # user in a half-configured state.
  if ! bash "$PLUGIN_ROOT/scripts/ensure-plugin-root.sh" "$PLUGIN_ROOT" --force; then
    log "WARNING: ensure-plugin-root failed; rerun manually:"
    log "  bash $PLUGIN_ROOT/scripts/ensure-plugin-root.sh $PLUGIN_ROOT --force"
  fi
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
fi

refresh_local_dashboard

log ""
if [ "$SETUP_CLAUDE_CODE" = "1" ] && [ "$SETUP_CODEX" = "1" ]; then
  log "done. Restart Claude Code and fully restart Codex to pick up the local plugin/hooks."
elif [ "$SETUP_CODEX" = "1" ]; then
  log "done. Fully restart Codex to pick up the local plugin/hooks."
else
  log "done. Restart Claude Code to pick up the local plugin."
fi
log "  pyproject.toml → editable reflexio-ai from ../reflexio"
log "  ~/.reflexio/.env → local-CLI + local-embedding providers"
if [ "$SETUP_CLAUDE_CODE" = "1" ]; then
  log "  Claude Code user marketplaces → reflexioai-local ($LOCAL_MKT_DIR)"
  log "  .claude/settings.local.json → claude-smart@reflexioai-local"
fi
if [ "$SETUP_CODEX" = "1" ]; then
  log "  Codex marketplace/cache/config → claude-smart@reflexioai"
  log "  Codex hooks → patched through scripts/codex-hook.js"
fi
