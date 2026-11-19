---
name: dashboard
description: Open the claude-smart dashboard (http://localhost:3001) in the browser, starting the backend and dashboard if they aren't running
allowed-tools: Bash(bash:*)
---

Run this bash command and show its output verbatim:

!`root="$HOME/.reflexio/plugin-root"; if [ ! -d "$root" ]; then IFS= read -r root <"$root.txt" || exit 1; if command -v cygpath >/dev/null 2>&1; then root="$(cygpath -u "$root")"; fi; fi; bash "$root/scripts/dashboard-open.sh"`
