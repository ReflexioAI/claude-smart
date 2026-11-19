---
name: show
description: Show current project-specific skills and session preferences
allowed-tools: Bash(bash:*)
---

Run this bash command and show its output verbatim:

!`root="$HOME/.reflexio/plugin-root"; if [ ! -d "$root" ]; then IFS= read -r root <"$root.txt" || exit 1; if command -v cygpath >/dev/null 2>&1; then root="$(cygpath -u "$root")"; fi; fi; bash "$root/scripts/cli.sh" show`
