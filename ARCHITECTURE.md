# Architecture

Technical reference for how `claude-smart` wires Claude Code's lifecycle hooks to the [reflexio](https://github.com/ReflexioAI/reflexio) backend to produce preferences, project-specific skills, and shared skills described in the [README](./README.md#how-it-works). If you're just using the plugin, the README is enough — this file is for people debugging, extending, or integrating with claude-smart.

## Core components

1. **6 lifecycle hooks** (`plugin/hooks/hooks.json`)
   - `SessionStart` — applies claude-smart/reflexio defaults and starts supporting services; it does not retrieve memory. On `source: "compact"` it marks the session so its next search resets server-side dedup (see [Per-session injection dedup](#per-session-injection-dedup)).
   - `UserPromptSubmit` — buffers each user turn, heuristically flags corrections, and searches reflexio with the prompt text to inject matching preference/skill hits as `additionalContext`.
   - `PreToolUse` — searches reflexio keyed on the first line of the tool-call text (Bash command, Edit `new_string`, etc.) and injects top matches as `additionalContext`. Codex registers it for `Bash` only: its PreToolUse `hookSpecificOutput.additionalContext` reaches the model, and shell calls arrive as `tool_name: "Bash"` with `tool_input.command`.
   - Both inject a skill-style **index**, not full items: one line per hit with its `[cs:ID]`, when it applies (the item's `trigger`, or the first line of its content when it has none, cut at a word boundary to 150 characters), and its link, under a header that names the loader. Each search asks for up to 5 hits per collection (`_TOP_K`), lowered so the three collections together return at most `CLAUDE_SMART_MAX_INJECTED_ITEMS` results (default 6, so 2 per collection; caps below 3 mean one per collection). The cap is enforced by requesting fewer results rather than dropping returned ones, because the server marks everything it returns as served (see below). `CLAUDE_SMART_SEARCH_THRESHOLD`, when set, is sent as the search similarity threshold.
   - The model loads an item's full text (content, trigger, rationale, link) on demand with `bash ~/.reflexio/plugin-root/scripts/cli.sh load --session <session-id> <id>`. The loader reads the session's citation registry (`<state>/<session>.injected.jsonl`), which caches each shown item, so it needs no backend call. A successful load also appends a `loaded_learning_refs` record to the session buffer. If the load lands in the same publish batch as the still-open turn that injected the learning, that turn's `retrieved_learnings` entry is published with `loaded: true` (once, however often it was loaded). A load never adds an entry, since an entry reads as an exposure, and a frozen retry must stay byte-identical. So a load of a learning from an earlier response, or across a reserved-response or frozen-retry boundary, is recorded locally only (`target: "published"`), and each load writes a `load` event with `load_sent` and the session's `local_only_loads` count to the hook log. The same command works from the shell tool of Claude Code, Codex and OpenCode.
   - The whole block stays within a per-host budget, header and footer included: `INLINE_CONTEXT_BUDGET_CHARS` (8,000) for Claude Code (10,000-character hook cap) and OpenCode (no documented cap), `CODEX_INLINE_CONTEXT_BUDGET_CHARS` (6,000) for Codex (about 2,500 tokens per hook output). Overflow drops the lowest-ranked lines. Codex's UserPromptSubmit gets the same index on one line because its TUI displays the context; OpenCode receives the hook's `additionalContext` through `plugin/opencode/server.mts` and appends it to the system prompt.
   - `PostToolUse` — records tool invocations for later extraction.
   - `Stop` — finalizes the assistant turn; Codex queues publication after local capture, while other hosts publish synchronously.
   - `SessionEnd` — publishes the remaining buffer without forcing extraction, then sends a completion hint even if all messages were already saved. The backend retains its usefulness judge.
2. **Local state buffer** — JSONL per session at `~/.claude-smart/sessions/{session_id}.jsonl`. Offline-safe.
3. **Reflexio backend** (`reflexio-ai` package, or a generated `plugin/vendor/reflexio` bundle in fast npm releases) — SQLite storage, hybrid search, preference/skill extraction, dedup, status lifecycle (`CURRENT` → `ARCHIVED`). Runs on `localhost:8071`.
4. **Local host LLM provider** — a LiteLLM custom provider registered inside reflexio. Every generation call (extraction, update, dedup, evaluation) subprocesses the active host CLI (`claude -p` under Claude Code, `codex exec` under Codex), so no OpenAI/Anthropic key is needed for the learning loop.

## Data flow

```
Claude Code session
  ├─ UserPromptSubmit ─┐
  ├─ PostToolUse  ─────┤  → JSONL buffer ─→ Stop ─→ reflexio publish_interaction
  └─ Stop         ─────┘                              │
                                                      ▼
                                        ┌─────────────────────────┐
                                        │ reflexio extractors     │
                                        │  (run via claude -p)    │
                                        │  → preferences + skills │
                                        └────────────┬────────────┘
                                                     │
                                                     ▼
Next user prompt / tool call → query-aware search for project-specific skills,
                              shared skills, and preferences
                           → additionalContext injected with matching hits
```

## Mapping to reflexio

### Codex publication ownership

Codex's Node launcher dispatches through the prepared `.venv` Python, bounded
below the host's prompt/tool and Stop deadlines. Native final-message payloads
avoid reading the transcript when the user prompt is already known; the existing
fallback retains internal-call filtering when it is missing.

Stop atomically records a private intent under `~/.claude-smart/publish-outbox/`.
The JSONL buffer remains the sole payload and frozen-request owner. Intents bind
the session/project, backend/account fingerprint, configuration digest and last
completed assistant record. A single detached worker drains these bounds through
the existing publisher. Session-level publication locks also serialize manual
callers. Enqueue never advances the acknowledgement watermark.

A queue gate, launch claim and generation comparison prevent duplicate launch
bursts, lost wakeups and deletion of a newer intent. The worker drains bounded
rounds and carries failed-generation exclusions through continuation claims,
so healthy work progresses without immediate repeated failures. Failed work waits for a later Stop
or SessionStart. Read-only mode stops dispatch. Changed configuration or a
different backend/account holds pending intents rather than redirecting them;
restore the matching context to recover them. No legacy buffers are swept and
no credentials or conversation content are copied into the outbox.

### Field mapping

| Reflexio field | claude-smart value |
| --- | --- |
| `user_id` | `project_id` (git-toplevel basename) — scopes preferences to the current project |
| `agent_version` | `claude-code` for shared skills; project-specific skills also carry project provenance |
| `session_id` | Claude Code `session_id` — for reflexio's deferred success evaluation, and sent on every hook search so the server dedups injections per session |

`plugin/src/claude_smart/reflexio_adapter.py` keeps user-facing behavior scoped clearly:

- **Preferences** are retrieved for the current project.
- **Project-specific skills** are retrieved for the current project.
- **Shared skills** are retrieved across projects and filtered to auto-generated or persisted statuses; rejected shared skills are not injected.

The adapter method names still mirror Reflexio's wire fields (`user_profiles`, `user_playbooks`, `agent_playbooks`) because those are backend API names, not user-facing terminology.

### Per-session injection dedup

Hook searches (UserPromptSubmit and PreToolUse) fire many times per session, so
`search_all` passes the Claude Code `session_id` to reflexio's `/api/search`.
The server remembers which rules it already returned to that session and skips
them on later searches, backfilling next-best matches instead — a turn with ten
tool calls injects each rule once, not ten times. The seen-state lives
in-memory on the backend (lost on restart, which only means an occasional
re-injection). Compaction keeps the `session_id`, so rules that fell out of the
model's context would stay withheld. Both Claude Code and Codex fire
`SessionStart` with `source: "compact"` after compacting; the handler writes
`<state>/<session>.reset-seen`, and the next hook search claims it and sends
`reset_session_seen: true` so the server forgets what it served that session.
A search that fails to reach the server restores the marker. The installed
Reflexio client must accept `reset_session_seen`; an older client cannot send
it, and the reset is skipped. Servers that predate the field ignore it.

The server marks every result it returns as served. The per-hook item cap
therefore lowers `top_k` instead of trimming results. The per-host character
budget still trims: a line it drops was marked served and is not offered again
in that session until a reset. Index lines are bounded, so this needs unusually
long labels at the default cap.

## Extraction signals

Reflexio's project-specific skill extractor only emits rules from two signals:

- **Correction SOPs** — the user rejected the agent's behavior (explicit `/learn`, or heuristically-detected corrective phrasing).
- **Success-path recipes** — completed tasks that produced concrete values, formulas, or tool sequences.

Identity/context facts (e.g. "I'm a data scientist", "we freeze merges Thursday") route to **preferences** only; they don't become skills. This is intentional — see Reflexio's skill extraction prompt for the full gating logic.
