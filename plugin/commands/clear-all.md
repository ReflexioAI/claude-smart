---
name: clear-all
description: Delete ALL local claude-smart learned data and restart the backend (destructive)
allowed-tools: Bash(bash:*)
---

Run this bash command and show its output verbatim:

!`launcher="$HOME/.claude-smart/guided-runtimes/guided-command.sh"; if [ -f "$launcher" ]; then bash "$launcher" cli.sh clear-all --yes; else root="$HOME/.reflexio/plugin-root"; if [ ! -d "$root" ]; then IFS= read -r root <"$root.txt" || exit 1; if command -v cygpath >/dev/null 2>&1; then root="$(cygpath -u "$root")"; fi; fi; bash "$root/scripts/cli.sh" clear-all --yes; fi`
