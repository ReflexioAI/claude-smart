# claude_smart (plugin/src)
Description: Python package powering the claude-smart plugin — hook handlers that buffer Claude Code / Codex session activity, publish it to a local Reflexio backend, and inject the learned skills/preferences back into future sessions.

> User-facing install/usage docs live in [`../README.md`](../README.md). This is the code map for the `claude_smart` package.

## Main Entry Points

| File | Purpose |
|------|---------|
| `hook.py` | Hook dispatcher. Parses the stdin JSON payload and routes each event (Setup, SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd) to its handler in `events/`. |
| `cli.py` | Prepared-runtime commands: `show`, `learn`, `load`, `restart`, `clear-all`, `clear-user`. Integration management belongs to `bin/claude-smart.js` (npm). |
| `env_config.py` | Dotenv readers and append-only bootstrap repair: native OS lock, private staging and atomic publication. All host preparation delegates missing-default repair here after core Python preparation; confirmed npm/dashboard choice writers remain separate. |
| `state.py` | Per-session JSONL buffer at `~/.claude-smart/sessions/{session_id}.jsonl`; high-water mark for idempotent publish retries. |
| `publish.py` | Drains the session buffer, calls the Reflexio adapter, and stamps the watermark. Used by Stop, SessionEnd, and `learn`. |
| `publish_outbox.py` | Private, destination-bound Codex publish intents and one detached worker; reuses the existing publisher and recovers on startup/Stop. |
| `reflexio_adapter.py` | Thin wrapper over `reflexio.ReflexioClient` — isolates import failures, 5s HTTP timeout, injects/queries learnings, degrades gracefully when the backend is down. |

## Event Handlers (`events/`)

| Handler | Hook | Role |
|---------|------|------|
| `session_start.py` | SessionStart | Recover Codex publish intents and render the stall banner; leave server configuration unchanged. |
| `user_prompt.py` | UserPromptSubmit | Inject learned playbooks/preferences into context. |
| `pre_tool.py` | PreToolUse (Edit/Write/Bash/NotebookEdit) | Inject project-specific context before tool execution. |
| `post_tool.py` | PostToolUse | Buffer the tool invocation (name, input, response, duration) into session state. |
| `stop.py` | Stop | Save the assistant turn; queue Codex publication, otherwise publish synchronously. |
| `session_end.py` | SessionEnd | Publish unsaved turns, then finalize the saved prefix through the judge; optional backend/dashboard shutdown. |

## Supporting Modules

| File | Purpose |
|------|---------|
| `runtime.py` | Host detection (Claude Code vs Codex); shared agent version. |
| `ids.py` | Session / project ID generation and resolution. |
| `context_inject.py`, `context_format.py`, `query_compose.py`, `cs_cite.py` | Build search queries, format learned skills as markdown, inject into context, format citations. |
| `stall_banner.py` | User-facing message when the Reflexio provider hits an auth/billing stall. |
| `hook_log.py`, `internal_call.py` | Structured JSON logging to the selected absolute session directory’s `hook.log`, or the account log at `~/.claude-smart/hook.log`; detect internal/test invocations to skip learning. |

## Architecture

```
SessionStart -> bootstrap Reflexio backend (8071) + Next.js dashboard (3001)
PostToolUse  -> state.py buffers each tool call to ~/.claude-smart/sessions/{id}.jsonl
Stop/SessionEnd -> publish.py -> reflexio_adapter -> Reflexio extracts playbooks + preferences
UserPromptSubmit / PreToolUse -> context_inject pulls relevant learnings back into context
```

- **Shared runtime** — Claude Code, Codex and OpenCode events are normalized in `runtime.py`; the npm CLI owns integration installation, updating and removal.
- **Offline-resilient** — if Reflexio is unreachable, interactions stay buffered and drain on the next successful publish.
- **Hooks registered** in `../hooks/hooks.json` (Claude Code) and `../hooks/codex-hooks.json` (Codex). Claude Code uses `../scripts/hook_entry.sh`; Codex uses `../scripts/codex-hook.js` and the prepared Python environment directly.
- **Codex deadlines** — Python dispatch has a 12s prompt/tool budget and 24s Stop budget. Publication runs outside Stop in one detached worker; acknowledgement still controls watermarks. Launcher, handler and worker timings distinguish slow stages; optional log writes skip contended locks.

## Requirements / Problems to Avoid

- **All learned state is external to the repo** — sessions buffer under `~/.claude-smart/`, Reflexio state under `~/.reflexio/`. Don't write generated runtime artifacts inside the plugin root.
- **Backend lives at `http://localhost:8071`**, dashboard at `http://localhost:3001`; both are long-lived across sessions (no hard shutdown on SessionEnd by default).
- **`CLAUDE_SMART_READ_ONLY`** skips publishing entirely — respect it in any new publish path.
- **Keep handlers fast** — tool-use hooks run on a 10–15s timeout; heavy work belongs in the backend, not the hook.

### Python settings publication

The internal `python -I -m claude_smart.env_config bootstrap-repair` operation
adds missing local defaults under an adjacent `.env.lock` after shell dependency
preparation. It preserves connection values, sharing, host assignments, caps,
comments and duplicates. Remote URL alone means attached mode: absent credentials
need authentication repair, not an automatic endpoint change. The repair step
neither prunes connections nor rewrites port 8081 or creates `.bak` files.

The transaction uses native locking with no soft fallback or application-side
marker deletion. A competing publisher gets a typed busy error; the shell exits
transiently without a persistent installation-failure marker. Ordinary failures
before atomic replacement preserve the old bytes. A declined remote publication
does not create `.env`. Filelock 4.2.10 provides the supported native API and
acquisition-failure cleanup; Python 3.12.4 provides private Windows staging
access. Windows read-only attributes are repaired before replacement. Process
death can leave a private staging directory, and the OS releases its lock.

This covers dependency repair, not the entire installation or every writer.
Node installer and dashboard configuration decisions remain separate. External
editors do not participate in the lock; directory/power-loss durability and
rollback after a committed replacement are not promised. The Python installer
and its exclusive helpers/setters were retired; use `npx claude-smart` for
integration management. Runtime/data/service commands remain in `cli.py`.


### Setup ownership

Public `npx claude-smart setup` uses the same guide and per-host install workers
as install/update. The former root Bash wizard and private setup worker were
retired; they no longer parse or write settings. Internal host workers accept
installation/removal operations, not another setup prompt flow.

The confirmed npm guide owns installation connection/learning/read-only/sharing
choices. Host workers read fresh prepared policy and preserve saved choices;
they never select a shared host from the integration being installed/removed.
The prepared Python owner supplies only missing local defaults under its native
lock, preserving present values and remote no-op behavior. Coding identity remains
per hook; saved learning intent remains independent, including managed storage
with a native optimizer. Existing legacy HOST text is preserved for the pinned
SDK, without being defaulted or rewritten during installation.
