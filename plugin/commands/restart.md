---
name: restart
description: Restart the reflexio backend and dashboard to pick up new changes
allowed-tools: Bash(bash:*)
---

Run this bash command and show its output verbatim:

!`launcher="$HOME/.claude-smart/guided-runtimes/guided-command.sh"; if [ -f "$launcher" ]; then bash "$launcher" cli.sh restart; else root="$HOME/.reflexio/plugin-root"; if [ ! -d "$root" ]; then IFS= read -r root <"$root.txt" || exit 1; if command -v cygpath >/dev/null 2>&1; then root="$(cygpath -u "$root")"; fi; fi; bash "$root/scripts/cli.sh" restart; fi`
