# claude_smart (plugin/src)
Description: Python package powering the claude-smart plugin — hook handlers that buffer Claude Code / Codex session activity, publish it to a local Reflexio backend, and inject the learned skills/preferences back into future sessions.

> User-facing install/usage docs live in [`../README.md`](../README.md). This is the code map for the `claude_smart` package.

## Main Entry Points

| File | Purpose |
|------|---------|
| `hook.py` | Hook dispatcher. Parses the stdin JSON payload and routes each event (Setup, SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd) to its handler in `events/`. |
| `cli.py` | `claude-smart` CLI: `install`, `uninstall`, `show`, `learn`, `restart`, `dashboard`, `clear-all`. |
| `state.py` | Per-session JSONL buffer at `~/.claude-smart/sessions/{session_id}.jsonl`; high-water mark for idempotent publish retries. |
| `publish.py` | Drains the session buffer, calls the Reflexio adapter, and stamps the watermark. Used by Stop, SessionEnd, and `learn`. |
| `publish_outbox.py` | Private, destination-bound Codex publish intents and one detached worker; reuses the existing publisher and recovers on startup/Stop. |
| `reflexio_adapter.py` | Thin wrapper over `reflexio.ReflexioClient` — isolates import failures, 5s HTTP timeout, injects/queries learnings, degrades gracefully when the backend is down. |

## Event Handlers (`events/`)

| Handler | Hook | Role |
|---------|------|------|
| `session_start.py` | SessionStart | Apply extraction defaults, render stall banner, push optimizer context. |
| `user_prompt.py` | UserPromptSubmit | Inject learned playbooks/preferences into context. |
| `pre_tool.py` | PreToolUse (Edit/Write/Bash/NotebookEdit) | Inject project-specific context before tool execution. |
| `post_tool.py` | PostToolUse | Buffer the tool invocation (name, input, response, duration) into session state. |
| `stop.py` | Stop | Save the assistant turn; queue Codex publication, otherwise publish synchronously. |
| `session_end.py` | SessionEnd | Publish unsaved turns, then finalize the saved prefix through the judge; optional backend/dashboard shutdown. |

## Supporting Modules

| File | Purpose |
|------|---------|
| `env_config.py` | Parses `~/.claude-smart/.env` (`REFLEXIO_URL`, `REFLEXIO_API_KEY`, `CLAUDE_SMART_READ_ONLY`, `CLAUDE_SMART_HOST`, local embedding/CLI flags). |
| `runtime.py` | Host detection (Claude Code vs Codex); shared agent version. |
| `ids.py` | Session / project ID generation and resolution. |
| `context_inject.py`, `context_format.py`, `query_compose.py`, `cs_cite.py` | Build search queries, format learned skills as markdown, inject into context, format citations. |
| `stall_banner.py` | User-facing message when the Reflexio provider hits an auth/billing stall. |
| `optimizer_assistant.py` | Claude-code CLI agent that extracts Reflexio optimization hints. |
| `hook_log.py`, `internal_call.py` | Structured JSON logging to `~/.claude-smart/hook.log`; detect internal/test invocations to skip learning. |

## Architecture

```
SessionStart -> bootstrap Reflexio backend (8071) + Next.js dashboard (3001)
PostToolUse  -> state.py buffers each tool call to ~/.claude-smart/sessions/{id}.jsonl
Stop/SessionEnd -> publish.py -> reflexio_adapter -> Reflexio extracts playbooks + preferences
UserPromptSubmit / PreToolUse -> context_inject pulls relevant learnings back into context
```

- **Dual-host** — the same package runs under Claude Code (native slash commands) and Codex (shell-script fallbacks in `../scripts/`); host shape is normalized in `runtime.py`.
- **Offline-resilient** — if Reflexio is unreachable, interactions stay buffered and drain on the next successful publish.
- **Hooks registered** in `../hooks/hooks.json` (Claude Code) and `../hooks/codex-hooks.json` (Codex). Claude Code uses `../scripts/hook_entry.sh`; Codex uses `../scripts/codex-hook.js` and the prepared Python environment directly.
- **Codex deadlines** — Python dispatch has a 12s prompt/tool budget and 24s Stop budget. Publication runs outside Stop in one detached worker; acknowledgement still controls watermarks. Launcher, handler and worker timings distinguish slow stages; optional log writes skip contended locks.

## Requirements / Problems to Avoid

- **All learned state is external to the repo** — sessions buffer under `~/.claude-smart/`, Reflexio state under `~/.reflexio/`. Don't write generated runtime artifacts inside the plugin root.
- **Backend lives at `http://localhost:8071`**, dashboard at `http://localhost:3001`; both are long-lived across sessions (no hard shutdown on SessionEnd by default).
- **`CLAUDE_SMART_READ_ONLY`** skips publishing entirely — respect it in any new publish path.
- **Keep handlers fast** — tool-use hooks run on a 10–15s timeout; heavy work belongs in the backend, not the hook.
