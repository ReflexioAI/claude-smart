#!/usr/bin/env bash
# Maintain ~/.reflexio/plugin-root as a symlink to the active plugin
# install dir so slash commands can reference one short path.
#
# Usage: ensure-plugin-root.sh <target-dir> [--force]
#   --force  overwrite any existing link
#   default  self-heal only if the link is missing or points at an
#            invalid target
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/_lib.sh" ]; then
    # shellcheck source=_lib.sh
    . "$HERE/_lib.sh"
fi

# This script can be invoked without plugin/scripts/_lib.sh; keep these fallbacks
# in sync with the shared helpers there.
if ! command -v claude_smart_is_windows >/dev/null 2>&1; then
    claude_smart_is_windows() {
        case "$(uname -s 2>/dev/null)" in
            MINGW*|MSYS*|CYGWIN*) return 0 ;;
            *) return 1 ;;
        esac
    }
fi

if ! command -v claude_smart_to_windows_path >/dev/null 2>&1; then
    claude_smart_to_windows_path() {
        path="$1"
        if command -v cygpath >/dev/null 2>&1; then
            cygpath -w "$path"
            return $?
        fi
        printf '%s\n' "$path" | awk '
        function slash_to_backslash(value) {
            gsub(/\//, "\\", value)
            return value
        }
        function drive_path(drive, rest) {
            drive = toupper(drive)
            sub(/^\//, "", rest)
            if (rest == "") {
                return drive ":\\"
            }
            return drive ":\\" slash_to_backslash(rest)
        }
        {
            normalized = $0
            gsub(/\\/, "/", normalized)
            if (normalized ~ /^[A-Za-z]:($|\/)/) {
                print drive_path(substr(normalized, 1, 1), substr(normalized, 3))
            } else if (normalized ~ /^\/cygdrive\/[A-Za-z]($|\/)/) {
                print drive_path(substr(normalized, 11, 1), substr(normalized, 12))
            } else if (normalized ~ /^\/mnt\/[A-Za-z]($|\/)/) {
                print drive_path(substr(normalized, 6, 1), substr(normalized, 7))
            } else if (normalized ~ /^\/[A-Za-z]($|\/)/) {
                print drive_path(substr(normalized, 2, 1), substr(normalized, 3))
            } else {
                print slash_to_backslash(normalized)
            }
        }'
    }
fi

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

write_plugin_root_metadata() {
    printf '%s\n' "$TARGET" >"$HOME/.reflexio/plugin-root.txt"
}

write_plugin_root_link() {
    reason="$1"
    if claude_smart_is_windows; then
        link_win="$(claude_smart_to_windows_path "$LINK")"
        target_win="$(claude_smart_to_windows_path "$TARGET")"
        if [ -L "$LINK" ] || [ -f "$LINK" ]; then
            rm -f "$LINK"
        elif [ -e "$LINK" ]; then
            # Intentionally avoid rmdir //S //Q here: if this is a real
            # directory rather than a junction, preserve it and use metadata
            # fallback instead of recursively deleting user-owned contents.
            if ! cmd.exe //C rmdir "$link_win" >/dev/null 2>&1; then
                echo "[claude-smart] ensure-plugin-root: could not remove existing Windows junction/path at $LINK" >&2
            fi
        fi
        if [ ! -e "$LINK" ] && cmd.exe //C mklink //J "$link_win" "$target_win" >/dev/null 2>&1; then
            write_plugin_root_metadata
            echo "[claude-smart] plugin-root → $TARGET${reason:+ ($reason)}" >&2
            return 0
        fi
        write_plugin_root_metadata
        if [ -e "$LINK" ]; then
            echo "[claude-smart] ensure-plugin-root: $LINK blocks Windows junction; preserving occupied path and wrote plugin-root.txt fallback" >&2
        else
            echo "[claude-smart] ensure-plugin-root: mklink //J failed; wrote plugin-root.txt fallback" >&2
        fi
        echo "[claude-smart] plugin-root.txt → $TARGET${reason:+ ($reason; junction unavailable)}" >&2
        return 0
    fi

    ln -sfn "$TARGET" "$LINK"
    echo "[claude-smart] plugin-root → $TARGET${reason:+ ($reason)}" >&2
}

if [ "$FORCE" = "--force" ]; then
    write_plugin_root_link "forced"
    exit 0
fi

# Opt-in: when CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION=1 (set in the
# environment or in ~/.claude-smart/.env), always relink to $TARGET so the
# symlink tracks the currently loaded plugin.
FOLLOW="${CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION:-}"
if [ -z "$FOLLOW" ] && [ -f "$HOME/.claude-smart/.env" ]; then
    FOLLOW="$(grep -E '^(export[[:space:]]+)?CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION=' "$HOME/.claude-smart/.env" \
        | tail -n1 | sed -E 's/^(export[[:space:]]+)?CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION=//')"
    # Strip a single pair of surrounding double or single quotes, if present.
    FOLLOW="${FOLLOW#\"}"; FOLLOW="${FOLLOW%\"}"
    FOLLOW="${FOLLOW#\'}"; FOLLOW="${FOLLOW%\'}"
fi
if [ "$FOLLOW" = "1" ]; then
    write_plugin_root_link "follow-session"
    exit 0
fi

# Cache-tracking: if the link currently resolves to a path under the
# managed plugin cache (~/.claude/plugins/cache/ or ~/.codex/plugins/cache/),
# always retarget it to $TARGET. Plugin updates leave old version
# directories behind, so a valid pyproject.toml at the stale target is
# not proof the link is fresh.
CURRENT=""
if [ -L "$LINK" ]; then
    # Literal target string, not realpath: we compare against what was written by ln -s.
    CURRENT="$(readlink "$LINK" 2>/dev/null || true)"
elif claude_smart_is_windows && [ -e "$LINK" ] && [ -f "$HOME/.reflexio/plugin-root.txt" ]; then
    # Windows junctions are not reported as symlinks by Git Bash, so use the
    # metadata written alongside successful junction creation.
    CURRENT="$(cat "$HOME/.reflexio/plugin-root.txt" 2>/dev/null || true)"
elif claude_smart_is_windows && [ -e "$LINK" ]; then
    echo "[claude-smart] ensure-plugin-root: $LINK is not a symlink and plugin-root.txt is missing; cannot determine whether the Windows plugin-root tracks the active plugin. Run ensure-plugin-root.sh with --force or delete the occupied path to recreate it." >&2
fi
if [ -n "$CURRENT" ]; then
    case "$CURRENT" in
        "$HOME/.claude/plugins/cache/"*|"$HOME/.codex/plugins/cache/"*)
            CURRENT_NORM="${CURRENT%/}"
            TARGET_NORM="${TARGET%/}"
            if [ "$CURRENT_NORM" != "$TARGET_NORM" ]; then
                write_plugin_root_link "cache-tracking, was $CURRENT"
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

write_plugin_root_link ""
