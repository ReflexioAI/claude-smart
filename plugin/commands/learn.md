---
description: Flag the last turn as a correction so reflexio learns from it
allowed-tools: Bash(bash:*)
argument-hint: [note]
---

Flag the user's previous turn as a correction and force reflexio to run extraction on this session's interactions now.
Run the bash command below and show its output verbatim.

!`bash "$HOME/.reflexio/plugin-root/scripts/cli.sh" learn "$ARGUMENTS"`
