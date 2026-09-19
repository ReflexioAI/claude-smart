# claude-smart distribution kit — Claude Code skills + agent learning (2026-07-20)

## Why this angle

Developers searching around Claude Code skills, Claude Code memory, and Codex memory usually have the same pain: the assistant repeats a correction or re-discovers a workflow every session. `claude-smart` should show up as the lightweight local learning layer for that problem.

Primary message:

> claude-smart turns corrections and successful workflows into durable Preferences, Project-specific skills, and Shared skills that Claude Code, Codex, and OpenCode reuse in future sessions.

This preserves the public terminology from `DEVELOPER.md` and avoids raw Reflexio storage/API names.

## High-signal audiences

1. **Claude Code plugin users** who already use slash commands/hooks and are trying to make Claude remember repo conventions.
2. **Codex users** who want local memory without introducing a hosted service or new API key.
3. **OpenCode users** evaluating plugins that help agents reuse project workflows.
4. **Agent-memory builders** comparing transcript memory with future-facing rules or skills.

## Suggested first-party post for GitHub Discussions / community changelog

Use only in first-party or clearly project-owned channels; do not drop into unrelated threads.

```text
We’ve been tightening the positioning for claude-smart around “learning, not just memory.”

The plugin runs locally for Claude Code, Codex, and OpenCode. It turns corrections and successful workflows into three durable artifacts:

- Preferences: project-scoped facts about how you work
- Project-specific skills: repo-local rules with triggers/rationales
- Shared skills: durable patterns that can transfer across projects

The practical outcome: future sessions start from the learned path instead of rediscovering the same test command, deploy caveat, dependency rule, or review preference.

Repo: https://github.com/ReflexioAI/claude-smart
Benchmark notes: https://github.com/ReflexioAI/claude-smart/blob/main/EXPERIMENT.md
```

## Suggested X post for Yi to review

No X action should be taken automatically.

```text
Most “AI coding memory” captures what happened.

The harder part is turning a correction into a rule the agent actually follows next time.

claude-smart does that for Claude Code, Codex, and OpenCode with:
- Preferences
- Project-specific skills
- Shared skills

Runs locally, powered by Reflexio.
https://github.com/ReflexioAI/claude-smart
```

## Suggested X reply for relevant conversations

Only use when someone is explicitly discussing Claude Code memory, repeated agent mistakes, persistent context, or agent learning from corrections.

```text
One useful distinction: memory stores what happened; learning stores what to do next time.

claude-smart turns corrections/workflows into Preferences, Project-specific skills, and Shared skills that Claude Code, Codex, and OpenCode can reuse in future sessions.

Repo: https://github.com/ReflexioAI/claude-smart
```

## Lightweight comparison-page outline

Working title: `Claude Code memory vs learning: why corrections should become skills`

1. **Problem:** transcript memory records context, but repeated mistakes persist when the next session lacks an actionable rule.
2. **Conceptual model:**
   - Preference = how this project/user works.
   - Project-specific skill = repo-local rule with trigger + rationale.
   - Shared skill = cross-project pattern distilled from repeated evidence.
3. **Example:** user corrects a test command, dependency policy, or deployment caveat; show memory-style note vs skill-style rule.
4. **Why local matters:** data and learned artifacts stay on the developer’s machine; no extra external API call for semantic search per README.
5. **Proof point:** summarize the benchmark carefully and link `EXPERIMENT.md`; avoid overstating beyond the documented synthetic benchmark.
6. **Install CTA:** `npx claude-smart install` plus Codex/OpenCode variants.

## Outreach guardrails

- Do not claim hosted sync, team sharing, enterprise controls, or package registry status beyond what the repo states.
- Tie benchmark claims directly to `EXPERIMENT.md`; do not generalize to all real-world sessions.
- Prefer educational framing over promotional replies.
- Do not post on X/Twitter automatically.
- Avoid competitor attacks; frame as “memory vs learning” rather than “tool X is bad.”

## Next owned-channel actions

1. Convert the comparison-page outline into a short docs/blog page if the project has an approved publishing destination.
2. Post the first-party update in a project-owned GitHub Discussion or changelog channel after review.
3. Use the X post manually from Yi’s account when there is a relevant release/update moment.
4. Reuse the reply only in high-signal conversations where someone is already asking for Claude Code memory or agent learning.
