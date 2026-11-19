---
name: learn
description: Force claude-smart to extract learnings from this session now
argument-hint: [optional note to capture]
allowed-tools: Bash(bash:*)
---

Force claude-smart to run extraction on this session's interactions now. If a
note is provided after the command, it is appended as a neutral User turn
before extraction so claude-smart can learn from it directly.
Run the bash command below and show its output verbatim.

!`launcher="$HOME/.claude-smart/guided-runtimes/guided-command.sh"; if [ -f "$launcher" ]; then bash "$launcher" cli.sh learn --note "$ARGUMENTS"; else root="$HOME/.reflexio/plugin-root"; if [ ! -d "$root" ]; then IFS= read -r root <"$root.txt" || exit 1; if command -v cygpath >/dev/null 2>&1; then root="$(cygpath -u "$root")"; fi; fi; bash "$root/scripts/cli.sh" learn --note "$ARGUMENTS"; fi`
