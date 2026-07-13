# claude-smart marketing brief — cross-host local learning (2026-07-13)

## Why this run

`origin/main` now clearly presents claude-smart as a learning layer for Claude Code, Codex, and OpenCode. The current public conversation around coding-agent memory is crowded with "shared memory", "second brain", and "MCP memory" positioning. The strongest low-risk angle for claude-smart is narrower and more differentiated:

> claude-smart turns corrections and successful workflows into local Preferences, Project-specific skills, and Shared skills that Claude Code, Codex, and OpenCode can reuse in future sessions.

This avoids generic memory claims while highlighting the parts developers can verify from the repo: local install, dashboard/auditability, host support, and rule-like learned artifacts.

## Read-only market signals checked

- GitHub repo metadata is aligned: description mentions Preferences, Project-specific skills, Shared skills, and topics include `claude-code-plugin`, `codex-plugin`, `opencode-plugin`, `agent-memory`, `ai-agent-memory`, `local-first`, and `self-improving-ai`.
- X/Twitter search was used read-only only. No likes/replies/posts/DMs were sent.
- Recent X results for `"Claude Code" memory`, `"Claude Code" plugin memory`, and `"Codex" memory agent` show active discussion around:
  - Claude Code agents forgetting between sessions.
  - Local/shared memory across Claude Code, Codex, and other coding agents.
  - Obsidian/second-brain memory workflows.
  - MCP-backed memory tools.

## Positioning recommendation

Lead with **learning from corrections**, not "another memory store".

Suggested hierarchy:

1. **Problem:** coding agents repeat the same mistakes across sessions and hosts.
2. **Mechanism:** claude-smart distills corrections and successful workflows into Preferences, Project-specific skills, and Shared skills.
3. **Distribution:** one local learning layer works across Claude Code, Codex, and OpenCode.
4. **Proof hooks:** inspectable dashboard, local storage, and benchmark in `EXPERIMENT.md` for correction capture vs `claude-mem`.

## Ready-to-post first-party update

Use after any active release/install issue is resolved, especially if issue #130 is still open when posting.

```text
Coding-agent memory should do more than remember a transcript.

The useful loop is:
correction → Project-specific skill → Shared skill → future Claude Code/Codex/OpenCode sessions start from the better path.

That is the design behind claude-smart: a local learning layer powered by Reflexio.

Repo: https://github.com/ReflexioAI/claude-smart
```

## Suggested X post for Yi to review

No X action was taken automatically.

```text
A pattern I keep seeing in coding-agent memory tools: they optimize for storing more context.

For coding, the higher-leverage unit is often a future-facing rule:
"when this situation appears again, do X instead of Y."

claude-smart turns corrections and successful workflows into Preferences, Project-specific skills, and Shared skills that Claude Code, Codex, and OpenCode can reuse locally.

https://github.com/ReflexioAI/claude-smart
```

## Suggested X reply for relevant threads

Only use when someone is explicitly asking about Claude Code/Codex/OpenCode memory or repeated agent mistakes. Do not use as a cold promo reply.

```text
One framing that helped us: store less transcript, more future-facing rule.

claude-smart turns corrections into Preferences, Project-specific skills, and Shared skills, so future Claude Code/Codex/OpenCode sessions can start from the learned path instead of rediscovering it.

Repo: https://github.com/ReflexioAI/claude-smart
```

## Non-X distribution opportunities

1. **GitHub issue #55 follow-up:** ask users to share one stale or useful Project-specific skill after the latest host-support changes. Keep it framed as product feedback, not marketing.
2. **Docs/blog outline:** "Memory vs learning for coding agents" — use examples already in `README.md` and tie benchmark claims only to `EXPERIMENT.md`.
3. **OpenCode communities:** if Yi has an owned/approved channel, share the one-sentence install update: `npx claude-smart install --host opencode` plus the note that local Preferences, Project-specific skills, and Shared skills are reused across supported hosts.

## Guardrails

- Do not claim hosted sync, team sharing, customer adoption, or benchmark results beyond `EXPERIMENT.md`.
- Do not post on X/Twitter automatically.
- Avoid competitor attacks; use the factual "memory vs learning" distinction.
- Preserve public terminology: Preference, Project-specific skill, Shared skill.
- If release/install issue #130 is still open, avoid broad launch amplification and keep messaging to docs/feedback channels until fixed.
