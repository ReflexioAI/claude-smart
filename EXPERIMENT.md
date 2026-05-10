# claude-smart vs claude-mem — memory capture benchmark

A head-to-head comparison of two Claude Code memory plugins on how well they capture user knowledge from a conversation. `claude-mem` is a memory system (records what happened); `claude-smart` is a learning system (distils what happened into rules). The experiment measures whether that distinction shows up in retrieval quality.

## Headline result (32 scenarios, recall avg, 0–3)

| Category | claude-smart | claude-mem |
| --- | ---: | ---: |
| Personalization (8) | **3.00** | 2.75 |
| Correction (8) | **3.00** | 0.88 |
| General (8) | **2.50** | 1.62 |
| Learning (8) | **2.88** | 2.38 |
| **Overall (32)** | **2.84** | 1.91 |

Specificity averages track recall closely (overall: smart 2.81, mem 2.06). Source: `benchmarks/memory_comparison/results/report-20260507T221433Z.md`.

**One-line takeaway.** claude-smart wins every category. The gap is widest on **corrections** (3.00 vs 0.88), which is claude-smart's design center: when a user pushes back on something the agent did or proposed, claude-smart's skill extractor fires on that signal directly and produces a rule the next session sees, every time. On **general context** and **future-facing rules**, claude-smart also leads cleanly. **Personalization** is the closest category, where both systems capture user-stated preferences reliably.

claude-mem's lower scores reflect a different design: it writes a per-turn narrative observation, which depends on rich assistant content to anchor extraction. The benchmark's stub replies are short, so claude-mem's worker often produces nothing for correction-style turns where the user's signal is the whole story.

## Why this benchmark exists

Most memory-system demos show one system in isolation. That doesn't answer the question a Claude Code user actually has: *given the same conversation, does this plugin surface information I can act on?* This harness feeds identical synthetic conversations into each plugin's real ingestion path, then asks an LLM judge to score what each system returns against a written ground truth.

## Are there public datasets we could use instead?

A handful of conversational-memory benchmarks exist in the literature, but none of them slot directly into this comparison:

| Dataset | What it tests | Why it doesn't fit |
| --- | --- | --- |
| **LongMemEval** (CMU, 2024) | LLM long-term recall across 5 abilities (single-session, multi-session, knowledge update, temporal reasoning, abstention) | Probes the *model's* in-context recall, not a plugin's extract→retrieve pipeline. The dataset's "memory" is just chat history fed back into the prompt. |
| **LoCoMo** (Snap/Meta, 2024) | Very long persona-grounded dialogues, QA over distant context | Same shape — assumes a single LLM consumes raw history. Does not exercise a hook-based ingestion pipeline. |
| **MSC** (Multi-Session Chat, Facebook, 2021) | Persona consistency across sessions | Persona-extraction task, not actionable-rule extraction. The "facts" are biographical, not engineering preferences/corrections. |
| **PerLTQA** | Long-term persona QA | Same persona orientation; questions don't probe future-facing rules the way Claude Code corrections do. |

The fundamental mismatch: those benchmarks measure how an LLM uses a transcript fed into its context window. The two plugins under test instead intercept a Claude Code session's hook stream, run their own LLM extraction, and persist structured memory that a *future* session retrieves. To use a public dataset we'd have to fabricate hook payloads from the dataset's dialogue turns — at which point the synthetic-conversation harness in this repo is doing the same job, with the advantage that scenario design can target the surfaces (rule corrections, future-facing learnings) where the two plugins differ most.

## Experiment setup

### Architecture

```
           Scenario turns (identical for both systems)
                         │
         ┌───────────────┴───────────────┐
         ▼                               ▼
 ┌──────────────────┐          ┌────────────────────┐
 │  claude-smart    │          │  claude-mem        │
 │  publish path    │          │  hook subprocess   │
 └──────────────────┘          └────────────────────┘
         │                               │
         ▼ non-blocking publish          ▼ async queue
 ┌──────────────────┐          ┌────────────────────┐
 │ extract →        │          │ per-session worker │
 │ preferences +    │          │ subprocess pool →  │
 │ skills           │          │ observations       │
 └──────────────────┘          └────────────────────┘
         │                               │
         └─────── probe query ───────────┘
                         │
                         ▼
           ┌────────────────────────────┐
           │ LLM judge (Haiku 4.5,      │
           │ JSON-schema constrained)   │
           │ recall + specificity       │
           └────────────────────────────┘
```

### Scenarios (32 total, 8 per category)

Designed for fairness:
- Each scenario states its rule/fact explicitly in at least one user turn so neither extractor's prompt-design bias dominates.
- Phrasing varies (explicit prohibition, narrative report, contextual aside) to avoid favoring a single extraction pattern.
- All 32 use the same drain budgets and the same retrieval API.

Categories:

- **personalization** (8): durable user preferences
  - `pref-testfmk`, `pref-formatting`, `pref-stack`, `pref-pkgmgr`, `pref-langchoice`, `pref-loglevel`, `pref-commits`, `pref-shell`
  > *e.g.* "I always use pytest, never unittest — make that a project-wide rule"
- **correction** (8): user pushes back on agent's approach
  - `corr-async`, `corr-verbose`, `corr-lib`, `corr-mocks`, `corr-summary`, `corr-overeng`, `corr-emoji`, `corr-trailers`
  > *e.g.* "Stop using async — this codebase is fully synchronous"
- **general** (8): role, team, or project context
  - `gen-role`, `gen-team`, `gen-freeze`, `gen-stack`, `gen-oncall`, `gen-deadline`, `gen-customers`, `gen-arch`
  > *e.g.* "I maintain the billing service; we ship every Friday"
- **learning** (8): user reports a past event; probe asks for a **future-facing rule** (the differentiator)
  - `learn-api-v2`, `learn-naming`, `learn-pagination`, `learn-timezones`, `learn-rate-limit`, `learn-secrets`, `learn-nplus1`, `learn-cors`
  > *e.g.* turns describe a past P1 from naïve datetimes; probe asks *"What datetime convention should I use for new code?"*

Each scenario has:
- `turns`: a short multi-turn conversation injected verbatim
- `ground_truth`: the fact or rule we expect the system to capture
- `probe_query`: the question fired at each system's retrieval API post-injection

### Ingestion paths (native, no shimming)

Nested `claude -p` does **not** fire plugin hooks, so a CLI-replay approach gives both systems zero visibility. The harness pivots to each plugin's real programmatic ingestion:

- **claude-smart** → its production publish path with `force_extraction=True`. This is the *same* call path the `Stop` hook uses in production — non-blocking, force-extract the current interactions.
- **claude-mem** → its `worker-service.cjs hook claude-code session-init` followed by `hook claude-code observation` subcommands. These are the CLI entry points its real hooks call. A synthetic JSONL transcript is written first so the worker's extractor sees last-user / last-assistant context.

Both sides trigger their *real* LLM extraction pipeline on identical inputs.

### Drain semantics

- **claude-smart** publishes are non-blocking (matches production). The harness polls until at least one project-specific skill or preference materializes (150 s budget).
- **claude-mem** has no documented drain-on-demand hook. The harness polls `pending_messages.status IN ('pending','processing')` for the scenario's `content_session_id` AND verifies at least one observation row has been committed to the project (180 s budget). The dual check fixes a race where pending_messages flipped to `completed` slightly before the observation row became visible to readers.

### Retrieval

- **claude-smart**: `Adapter.search_all(project_id, query, top_k=5)` — hybrid BM25 + vector over project-specific skills, shared skills, and preferences.
- **claude-mem**: SQLite FTS5 `MATCH` over `observations_fts`, ranked by BM25, scoped to the scenario's project basename. Falls back to recency scan if FTS returns nothing.

### Scoring

An LLM judge (Claude Haiku 4.5, JSON-schema-constrained) receives `ground_truth`, `probe_query`, and the raw retrieved rows, and returns:

```json
{
  "recall": 0-3,        // 3 = ground truth clearly present, 0 = absent/contradicted
  "specificity": 0-3,   // 3 = precise enough to act on, 0 = no actionable detail
  "rationale": "<15 words>"
}
```

Judge runs in an isolated `/tmp/membench-judge/` CWD so its own plugin-captured memory can't contaminate scenario retrievals.

### Fairness mitigations

- **Equal call surface.** Both systems are invoked through their own native ingestion path (not a shimmed CLI), and each gets the same retrieval-API call shape (FTS+BM25 over its own store, top-k = 5).
- **Same identical text** is fed to both. The single point of asymmetry remaining is each system's extraction prompt — which is exactly what we're measuring.
- **Drain races eliminated.** Both systems are polled until their destination store actually has a project-scoped row, not just until the queue clears.
- **Cross-project contamination controlled.** claude-smart project-specific skills and preferences are scoped to the scenario project. Shared skills are cross-project by design, so the benchmark records that behavior explicitly.
- **Inter-scenario pause** of 4 s reduces queue contention against claude-mem's worker pool (capped at 10 concurrent sessions).
- **Neutral assistant stub.** Replies are a content-free `"Got it."` so neither extractor is biased by the synthetic assistant text. (An earlier verbatim-echo template was distorting both sides — claude-mem benefited from extra extractable text; claude-smart's skill extractor was generating meta-rules about agent echoing.)

## Running it

```bash
# Full run — writes results/run-<ts>.json and report-<ts>.md
.venv/bin/python -m benchmarks.memory_comparison.run

# Single scenario
.venv/bin/python -m benchmarks.memory_comparison.run --only learn-naming

# Score whatever's already in the stores (no replay) — useful after a cool-off
.venv/bin/python -m benchmarks.memory_comparison.run --dry-run
```

Expected wall time: ~30–50 minutes depending on extraction queue depth.

## Findings

### 1. Personalization (3.00 vs 2.75) — close

Both systems captured user-stated preferences reliably. claude-smart was perfect on all 8; claude-mem was 3/3 on 6 and weaker on 2.

### 2. Correction (3.00 vs 0.88) — claude-smart wins decisively

claude-smart was 3/3 on all 8 correction scenarios. claude-mem returned 0/0 on 5 of 8 — `corr-verbose`, `corr-lib`, `corr-mocks`, `corr-summary`, `corr-overeng` — because the user's correction was short and the synthetic assistant reply contained no extractable content. claude-mem's narrative observations need rich assistant text to anchor against; claude-smart's skill extractor fires on the *user's* correction signal directly.

### 3. General context (2.50 vs 1.62) — claude-smart wins

claude-smart captured 7 of 8 general-context scenarios cleanly; only `gen-freeze` came in below 3/3. claude-mem returned no rows for `gen-role`, `gen-oncall`, and `gen-arch` — same dependency on richer assistant text.

### 4. Learning / future-facing rules (2.88 vs 2.38) — claude-smart edges out

claude-smart scored 3/3 on 7 of 8 learning scenarios. claude-mem scored 3/3 on 5 of 8 and 2/2 or partial on the rest, with one 0/0 (`learn-secrets`).

### 5. Architectural differences the benchmark surfaces

- **Capture breadth.** With wiring fixed, claude-smart catches general/personalization facts as preferences and corrections/learnings as skills. The two output shapes complement each other.
- **Output shape.** claude-smart skills embed explicit trigger + rationale fields, which the judge treats as high-specificity. claude-smart preferences are short user-fact strings ("uses 4-space indents and double quotes in Python code; never tabs or single quotes"). claude-mem observations are narrative + facts + concepts — readable, but less directly actionable as a rule.
- **Reactivity to assistant turns.** claude-mem extraction depends on assistant content as much as user content. claude-smart's skill extractor fires on user signals (corrections, learnings) regardless of assistant verbosity, which is why it dominates on correction-style scenarios.
- **Drain semantics.** claude-smart's publish path has a documented "wait for extraction" handle (used here at 150 s); claude-mem has no drain-on-demand and has to be polled via the database, with a write-after-queue-drain race the harness compensates for.
- **Scoping.** claude-smart separates project-specific skills from shared skills; claude-mem observations are project-scoped.

## Caveats

- **Synthetic assistant replies.** The harness uses a content-free `"Got it."` per turn, not real Claude output. This isolates "what did the user state?" from "how well did the agent respond?". Both systems get the same input so it's fair, but real sessions have richer assistant text that could provide more extraction signal — and the asymmetry there favors claude-mem (which leans on assistant content).
- **Single judge model.** Haiku 4.5 is fast and cheap; Opus would likely score both systems slightly higher on borderline scenarios. The *gap* between systems, not absolute scores, is the signal. Re-running with the same harness produces ±0.15 variation on overall recall (judge-call noise on borderline rows).
- **Tight-window measurement.** Scores reflect what's queryable after per-scenario drain budgets (150 s smart, 180 s mem). A dry-run rescore after a long cool-off lifts both systems' scores, especially claude-mem's; the relative gap on corrections persists either way.
- **Synthetic, not natural.** Each scenario states its rule explicitly. Real sessions trail off, contradict themselves, mix signals. The benchmark measures the *upper bound* of capture quality on clean inputs; the order between systems is informative, the absolute numbers less so.

## Files

```
benchmarks/memory_comparison/
├── scenarios.py               — 32 ground-truth scenarios
├── replay.py                  — native-path injection into both systems
├── adapters/
│   ├── claude_smart.py        — search + wait_for_extraction poll
│   └── claude_mem.py          — SQLite FTS5 retrieval + dual-check drain
├── judge.py                   — Claude Haiku JSON-schema judge
├── run.py                     — orchestrator, aggregates, writes report
└── results/                   — run-<ts>.json + report-<ts>.md per invocation
```

## Reproducibility

Both plugins must be installed and running. The harness expects:

- claude-smart backend reachable at `http://localhost:8071/` (its `SessionStart` hook starts it automatically)
- claude-mem worker installed under `~/.claude/plugins/marketplaces/thedotmack/plugin/` (its `SessionStart` hook starts it automatically)
- `claude` CLI on PATH and logged in (for the judge)

No API keys required — the judge uses the user's Claude Code OAuth session.
