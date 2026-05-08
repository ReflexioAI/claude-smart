# Troubleshooting

**SessionStart injects nothing after a correction.**
Extraction is async by default. Run `/learn` to flag the previous turn as a correction and force extraction, wait ~20–30s, then run `/show` — no new session needed. `/show` shows whether the rule was actually extracted.

**Reflexio refuses to boot with "no embedding-capable provider".**
Check that `CLAUDE_SMART_USE_LOCAL_EMBEDDING=1` is in `~/.reflexio/.env` *and* that `chromadb` is installed in the venv (`uv run --project plugin python -c "import chromadb"` should print nothing). If you'd rather use a cloud embedder instead, drop the env flag and set `OPENAI_API_KEY` or `GEMINI_API_KEY` in the same file.

**`claude-smart` doesn't see my interactions.**
Check `~/.claude-smart/sessions/`. If your current session's JSONL has no `User`/`Assistant` rows, the plugin isn't receiving hook events — verify `.claude/settings.local.json` has the right path and that `enabledPlugins` is `true`.

**Hooks appear to time out.**
Each hook is capped at 10–60s (see `plugin/hooks/hooks.json`). If you see long pauses, check `uv` is on PATH — hooks shell out to `uv run`.

**Dashboard says npm or Node is missing.**
The Setup hook should install a private Node.js/npm runtime under `~/.claude-smart/node/current` when no suitable global Node.js is available. If private Node setup or dashboard build fails, claude-smart writes the non-fatal marker `~/.claude-smart/dashboard-unavailable`; `/claude-smart:dashboard` prints that file before log tails. Restart Claude Code to retry Setup, or install Node.js 20.9+ manually and run `/claude-smart:restart`.

**Install reports `install-failed`.**
`~/.claude-smart/install-failed` is reserved for core setup failures such as `uv` installation or `uv sync --locked --python 3.12`. Fix the reported issue, delete the marker, then restart Claude Code so Setup can retry. Dashboard-only issues should appear in `~/.claude-smart/dashboard-unavailable` instead.

**Where are private install tools stored?**
`uv` is installed into the standard Astral locations (`~/.local/bin` or `~/.cargo/bin`). The private dashboard runtime is at `~/.claude-smart/node/current`. Set `CLAUDE_SMART_NODE_LTS_MAJOR=22` to choose the Node LTS major used by the private bootstrap.

**A different LLM is being used.**
Reflexio's provider priority is `claude-code > local > anthropic > gemini > ... > openai`. If you have `CLAUDE_SMART_USE_LOCAL_CLI=1` *and* an Anthropic key set, claude-code still wins for generation; `local` sits above openai/gemini for embeddings. Check the startup log line `Primary provider for generation: <name>` and `Embedding provider: <name>` to confirm.

**I want to wipe everything and start over.**
```bash
rm -rf ~/.claude-smart/sessions/
rm -rf ~/.reflexio/data/           # reflexio SQLite store
```
