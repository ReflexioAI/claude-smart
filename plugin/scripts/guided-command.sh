#!/usr/bin/env bash
# Dispatch account commands through the guide's private runtime selection.
set -eu

script="${1:-}"
case "$script" in
    cli.sh|dashboard-open.sh) shift ;;
    *) echo "Unknown claude-smart account command." >&2; exit 1 ;;
esac

state="$HOME/.claude-smart/guided-runtimes"
selection="$state/selected"
if [[ -e "$selection" || -L "$selection" ]]; then
    if [[ -f "$selection" && ! -L "$selection" ]]; then
        id=$(<"$selection")
        if [[ "$id" =~ ^runtime-[A-Za-z0-9]{6}$ &&
              ! -L "$state/$id" && ! -L "$state/$id/plugin" &&
              -f "$state/$id/plugin/scripts/$script" ]]; then
            exec bash "$state/$id/plugin/scripts/$script" "$@"
        fi
    fi
    echo "The guided runtime is unavailable. Rerun npx claude-smart setup." >&2
    exit 1
fi

root="$HOME/.reflexio/plugin-root"
if [[ ! -d "$root" ]]; then
    IFS= read -r root <"$root.txt" || exit 1
    if command -v cygpath >/dev/null 2>&1; then
        root="$(cygpath -u "$root")"
    fi
fi
exec bash "$root/scripts/$script" "$@"
