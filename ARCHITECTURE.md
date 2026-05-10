# Architecture

Technical reference for how `claude-smart` wires Claude Code's lifecycle hooks to the [reflexio](https://github.com/ReflexioAI/reflexio) backend to produce preferences, project-specific skills, and shared skills described in the [README](./README.md#how-it-works). If you're just using the plugin, the README is enough — this file is for people debugging, extending, or integrating with claude-smart.

## Core components

1. **6 lifecycle hooks** (`plugin/hooks/hooks.json`)
   - `SessionStart` — fetches skills from reflexio and injects them as `additionalContext`.
   - `UserPromptSubmit` — buffers each user turn, heuristically flags corrections, and searches reflexio with the prompt text to inject matching preference/skill hits as `additionalContext`.
   - `PreToolUse` — searches reflexio keyed on the first line of the tool-call text (Bash command, Edit `new_string`, etc.) and injects top matches as `additionalContext`.
   - `PostToolUse` — records tool invocations for later extraction.
   - `Stop` — finalizes the assistant turn from the transcript, publishes to reflexio.
   - `SessionEnd` — flushes the remaining buffer with `force_extraction=True`.
2. **Local state buffer** — JSONL per session at `~/.claude-smart/sessions/{session_id}.jsonl`. Offline-safe.
3. **Reflexio backend** (submodule at `reflexio/`) — SQLite storage, hybrid search, preference/skill extraction, dedup, status lifecycle (`CURRENT` → `ARCHIVED`). Runs on `localhost:8071`.
4. **Claude Code LLM provider** — a LiteLLM custom provider registered inside reflexio. Every generation call (extraction, update, dedup, evaluation) subprocesses `claude -p --output-format json`, so no OpenAI/Anthropic key is needed for the learning loop.

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
Next session → SessionStart → fetch project-specific skills, shared skills,
              and preferences → additionalContext injected into Claude's
              system prompt
```

## Mapping to reflexio

| Reflexio field | claude-smart value |
| --- | --- |
| `user_id` | `project_id` (git-toplevel basename) — scopes preferences to the current project |
| `agent_version` | `claude-code` for shared skills; project-specific skills also carry project provenance |
| `session_id` | Claude Code `session_id` — for reflexio's deferred success evaluation |

`plugin/src/claude_smart/reflexio_adapter.py` keeps user-facing behavior scoped clearly:

- **Preferences** are retrieved for the current project.
- **Project-specific skills** are retrieved for the current project.
- **Shared skills** are retrieved across projects and filtered to auto-generated or persisted statuses; rejected shared skills are not injected.

The adapter method names still mirror Reflexio's wire fields (`user_profiles`, `user_playbooks`, `agent_playbooks`) because those are backend API names, not user-facing terminology.

## Extraction signals

Reflexio's project-specific skill extractor only emits rules from two signals:

- **Correction SOPs** — the user rejected the agent's behavior (explicit `/learn`, or heuristically-detected corrective phrasing).
- **Success-path recipes** — completed tasks that produced concrete values, formulas, or tool sequences.

Identity/context facts (e.g. "I'm a data scientist", "we freeze merges Thursday") route to **preferences** only; they don't become skills. This is intentional — see Reflexio's skill extraction prompt for the full gating logic.
