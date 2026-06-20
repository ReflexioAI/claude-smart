---
name: sync-agent-instructions
description: Sync AI coding tool instruction files (CLAUDE.md, GEMINI.md, AGENTS.md) so they stay aligned. Detects which file changed and copies it to the others. Use when syncing md files, syncing instructions, updating GEMINI.md, or updating AGENTS.md.
---

# Sync Agent Instructions

Keep CLAUDE.md, GEMINI.md, and AGENTS.md in sync so all AI coding tools share the same project instructions.

## Workflow

1. **Detect changes** — Run `git diff HEAD -- CLAUDE.md GEMINI.md AGENTS.md` to see which file(s) have uncommitted changes.

2. **Determine source file:**
   - **One file changed** — That file is the source. Copy its content to the other two.
   - **Multiple files changed** — Ask the user which file to use as the source.
   - **No files changed** — Compare all three with `diff`. If they differ, report which sections differ and ask the user which file to use as source. If identical, report "All instruction files are in sync." and stop.

3. **Sync** — Read the source file and write its exact content to the other two files.

4. **Stage** — Run `git add` on the two modified files.

5. **Report** — Tell the user which file was the source and that the other two have been updated.
