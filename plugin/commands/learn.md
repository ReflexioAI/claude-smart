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

!`bash "$HOME/.reflexio/plugin-root/scripts/cli.sh" learn --note "$ARGUMENTS"`
