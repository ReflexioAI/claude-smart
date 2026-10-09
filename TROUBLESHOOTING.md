# Troubleshooting

**SessionStart injects nothing after a correction.**
Extraction is async by default. Run `/learn` to flag the previous turn as a correction and force extraction, wait ~20–30s, then run `/show` — no new session needed. `/show` shows whether the rule was actually extracted.

**Reflexio refuses to boot with "no embedding-capable provider".**
The default local setup starts `reflexio embeddings serve` on `127.0.0.1:8072` with the shared backend. Check `~/.claude-smart/backend.log` for the `embedding` service startup line and fix any error shown there.

**`claude-smart` doesn't see my interactions.**
Check `~/.claude-smart/sessions/`. If your current session's JSONL has no `User`/`Assistant` rows, the plugin isn't receiving hook events — verify `.claude/settings.local.json` has the right path and that `enabledPlugins` is `true`.

**Hooks appear to time out.**
Each hook is capped at 10–60s (see `plugin/hooks/hooks.json`). If you see long pauses, check `uv` is on PATH — hooks shell out to `uv run`.

**Dashboard says npm or Node is missing.**
The Setup hook should install a private Node.js/npm runtime under `~/.claude-smart/node/current` when no suitable global Node.js is available. If private Node setup or dashboard build fails, claude-smart writes the non-fatal marker `~/.claude-smart/dashboard-unavailable`; `/claude-smart:dashboard` prints that file before log tails. Restart Claude Code to retry Setup, or install Node.js 20.9+ manually and run `/claude-smart:restart`.

**OpenCode on Windows injects nothing or never learns.**
`claude-smart install --host opencode` fails early if OpenCode, Git Bash, or the Windows local embedding runtime is missing. Fix the message in `~/.claude-smart/install-failed`, rerun install, then check `~/.claude-smart/backend.log` for bridge errors from `opencode-claude-compat.cmd` if learning still does not run.

**Windows install fails with an onnxruntime or Visual C++ Redistributable message.**
The default local semantic search uses `onnxruntime`, which loads Microsoft native runtime DLLs on Windows. Install the x64 Microsoft Visual C++ Redistributable from `https://aka.ms/vs/17/release/vc_redist.x64.exe`, then rerun `claude-smart install`.

**Install reports `install-failed`.**
`~/.claude-smart/install-failed` is reserved for core setup failures such as `uv` installation or `uv sync --locked --python 3.12`. Fix the reported issue, delete the marker, then restart Claude Code so Setup can retry. Dashboard-only issues should appear in `~/.claude-smart/dashboard-unavailable` instead.

**Backend log repeats `bundled Reflexio import preflight failed`.**
This means claude-smart was installed from the GitHub marketplace, whose
checkout intentionally excludes the generated Reflexio runtime bundle. Current
releases make that install impossible (`claude plugin marketplace add
ReflexioAI/claude-smart` now fails with `Marketplace file not found`), but an
install from an older release can still be in this state. Repair the cache from
the packaged npm artifact:

```bash
npx claude-smart update
```

The installer replaces an incomplete cache for the version it is installing.
Restart Claude Code afterward. Do not use `claude plugin marketplace add
ReflexioAI/claude-smart`; use the npm command for installation and updates.

`/claude-smart:restart` cannot repair this — it preflights the same missing
bundle and deliberately leaves your running services alone rather than stopping
a backend it cannot replace. Use `npx claude-smart update`.

**Where are private install tools stored?**
`uv` is installed into the standard Astral locations (`~/.local/bin` or `~/.cargo/bin`). The private dashboard runtime is at `~/.claude-smart/node/current`. Set `CLAUDE_SMART_NODE_LTS_MAJOR=22` to choose the Node LTS major used by the private bootstrap.

**A different LLM is being used.**
Reflexio's provider priority is `claude-code > local > anthropic > gemini > ... > openai`. If you have `CLAUDE_SMART_USE_LOCAL_CLI=1` *and* an Anthropic key set, claude-code still wins for generation; `local` sits above openai/gemini for embeddings. Check the startup log line `Primary provider for generation: <name>` and `Embedding provider: <name>` to confirm.

**I want to wipe everything and start over.**
```bash
rm -rf ~/.claude-smart/sessions/
rm -rf ~/.reflexio/data/           # reflexio SQLite store
```

### Diagnose automatic publishing failures

`~/.claude-smart/hook.log` includes `publish-result` records with the plugin version,
backend scheme/hostname/port, publish count, exception class, and HTTP status when
available. Missing or malformed version metadata uses `unknown`; unavailable
URL metadata uses null fields. These records omit credentials, URL paths/query parameters, response
bodies, and interaction content. A logging failure never changes publication success.
Compare the recorded destination with `~/.claude-smart/.env` when a manual command
works but automatic publishing fails; an older host plugin may still read the legacy
`~/.reflexio/.env`. Update that host's installation and start a fresh session.

Publication requires an explicit `success: true` acknowledgement. HTTP 200
rejections, empty responses, or malformed success fields leave the buffer
retryable unless exact stored data can be verified. Legacy "Interaction queued
for processing" responses also require stored-request confirmation because their
background task may not have saved anything yet. Publishing stays asynchronous
and does not wait for extraction or poll for storage. Field-drop warnings from
every chunk remain observable even if a later chunk fails. Null characters in
captured text are represented as literal `\u0000` escapes for PostgreSQL storage. Messages with more than 1,000 tool
entries use labelled continuation messages so all tools remain visible to learning.
Requests contain at most 1,000 messages; larger batches use stable per-request IDs
and advance the local watermark only after every request is accepted. Failed batches
retry with the same IDs. Before replaying a request in a large batch, the plugin
queries that exact request and verifies its stored content, tools, and learning
links; matching accepted requests are skipped. Single requests also use this
confirmation after a rejected or lost acknowledgement. Verified recovery confirms
storage only: the read API cannot prove or rerun forced extraction, stall override,
or aggregation options. `/learn` reports this limitation and exits nonzero;
storage recovery still advances the local watermark. Run `/learn` again without
adding another note to publish any remaining buffered interactions or an existing
pending note. If none remain, a new real interaction or note can request extraction.
A fresh acknowledgement reports the extraction request was accepted. Preflight read failures
allow the original write to proceed; rejected or lost acknowledgements still
require verified storage before advancing. Failed verification, mismatched data,
or nonempty fields absent from the read API (citations and image encoding) leave
the batch retryable. Recovery requires a backend/read
contract that exposes the submitted fields; read APIs that omit nonempty learning
links cannot confirm their storage. Known tool status and learning-link fields
are verified from the raw response when the installed SDK predates them. Legacy
responses that omit status retain their original compatibility behavior. Stable
raw retries keep the original payload and learning links unchanged; an unconfirmed failure waits for the next
retry instead of sending a stripped payload. All accepted request IDs are retained
in the local watermark. The dashboard uses every accepted request ID for host
attribution, including earlier chunks. Null escaping also covers nested mapping
keys and tuple values; a key collision fails before sending instead of losing data.
Empty placeholders are filtered before splitting; an empty-only batch advances
the local watermark without sending a request. Original local records are retained.
