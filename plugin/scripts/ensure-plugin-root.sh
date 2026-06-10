#!/usr/bin/env bash
# Maintain ~/.reflexio/plugin-root as a symlink to the active plugin
# install dir so slash commands can reference one short path.
#
# Usage: ensure-plugin-root.sh <target-dir> [--force]
#   --force  overwrite any existing link
#   default  self-heal only if the link is missing or points at an
#            invalid target
set -eu

TARGET="${1:-}"
FORCE="${2:-}"

if [ -z "$TARGET" ]; then
    echo "[claude-smart] ensure-plugin-root: usage: $0 <target-dir> [--force]" >&2
    exit 1
fi

if [ ! -f "$TARGET/pyproject.toml" ]; then
    echo "[claude-smart] ensure-plugin-root: $TARGET is not a plugin dir (no pyproject.toml)" >&2
    exit 1
fi
TARGET="$(cd "$TARGET" && pwd -P)"

LINK="$HOME/.reflexio/plugin-root"
mkdir -p "$(dirname "$LINK")"

if [ "$FORCE" = "--force" ]; then
    ln -sfn "$TARGET" "$LINK"
    echo "[claude-smart] plugin-root → $TARGET (forced)" >&2
    exit 0
fi

# Opt-in: when CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION=1 (set in the
# environment or in ~/.reflexio/.env), always relink to $TARGET so the
# symlink tracks the currently loaded plugin.
FOLLOW="${CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION:-}"
if [ -z "$FOLLOW" ] && [ -f "$HOME/.reflexio/.env" ]; then
    FOLLOW="$(grep -E '^CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION=' "$HOME/.reflexio/.env" \
        | tail -n1 | cut -d= -f2-)"
    # Strip a single pair of surrounding double or single quotes, if present.
    FOLLOW="${FOLLOW#\"}"; FOLLOW="${FOLLOW%\"}"
    FOLLOW="${FOLLOW#\'}"; FOLLOW="${FOLLOW%\'}"
fi
if [ "$FOLLOW" = "1" ]; then
    ln -sfn "$TARGET" "$LINK"
    echo "[claude-smart] plugin-root → $TARGET (follow-session)" >&2
    exit 0
fi

# Cache-tracking: if the link currently resolves to a path under the
# managed plugin cache (~/.claude/plugins/cache/ or ~/.codex/plugins/cache/),
# always retarget it to $TARGET. Plugin updates leave old version
# directories behind, so a valid pyproject.toml at the stale target is
# not proof the link is fresh.
if [ -L "$LINK" ]; then
    # Literal target string, not realpath: we compare against what was written by ln -s.
    CURRENT="$(readlink "$LINK" 2>/dev/null || true)"
    case "$CURRENT" in
        "$HOME/.claude/plugins/cache/"*|"$HOME/.codex/plugins/cache/"*)
            CURRENT_NORM="${CURRENT%/}"
            TARGET_NORM="${TARGET%/}"
            if [ "$CURRENT_NORM" != "$TARGET_NORM" ]; then
                ln -sfn "$TARGET" "$LINK"
                echo "[claude-smart] plugin-root → $TARGET (cache-tracking, was $CURRENT)" >&2
            fi
            exit 0
            ;;
    esac
fi

# Self-heal path: only rewrite the link if it's missing or its target is
# gone/invalid.
if [ -f "$LINK/pyproject.toml" ]; then
    exit 0
fi

ln -sfn "$TARGET" "$LINK"
echo "[claude-smart] plugin-root → $TARGET" >&2
