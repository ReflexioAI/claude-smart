---
name: claude-smart
description: Codex-only — use when the user asks to run claude-smart commands in Codex, including claude-smart show, learn, restart, dashboard, clear-all, or slash-like requests such as /claude-smart:learn. In Claude Code the native plugin slash commands handle these; do not invoke this skill there.
---

# claude-smart Commands In Codex

Codex does not currently support plugin-provided slash commands. When the user
asks for a claude-smart command, run the equivalent shell command through the
active plugin root. This skill is Codex-specific; under Claude Code the native
`/claude-smart:*` slash commands already exist, so do not fall back to the
shell commands there.

## Command Map

- Dashboard: `bash ~/.reflexio/plugin-root/scripts/dashboard-open.sh`
- Show learned state: `bash ~/.reflexio/plugin-root/scripts/cli.sh show`
- Learn from the latest turn: `bash ~/.reflexio/plugin-root/scripts/cli.sh learn`
- Learn with a note: `bash ~/.reflexio/plugin-root/scripts/cli.sh learn --note "<note>"`
- Restart backend/dashboard: `bash ~/.reflexio/plugin-root/scripts/cli.sh restart`
- Clear all learned state: `bash ~/.reflexio/plugin-root/scripts/cli.sh clear-all --yes`

## Behavior

1. Treat slash-like prompts such as `/claude-smart:show` as requests to run the
   matching shell command above.
2. For `learn`, preserve any user-provided note exactly as the note text.
3. For `clear-all`, require explicit confirmation before running it because it
   deletes all reflexio interactions, preferences, and skills.
4. If `~/.reflexio/plugin-root` is missing or broken, tell the user to restart
   Codex after installing claude-smart, then rerun the command.
5. After running a command, summarize the important output concisely.
