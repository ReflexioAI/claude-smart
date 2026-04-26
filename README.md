<p align="center">
  <img src="assets/claude-smart-icon.png" alt="claude-smart" width="140">
</p>

<h1 align="center">
  claude-smart
</h1>

<h4 align="center">The <a href="https://claude.com/claude-code" target="_blank">Claude Code</a> plugin that makes Claude Code self-improve as you use it — so Claude Code stops repeating mistakes and gets better every session.</h4>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  </a>
  <a href="plugin/pyproject.toml">
    <img src="https://img.shields.io/badge/version-0.1.17-green.svg" alt="Version">
  </a>
  <a href="plugin/pyproject.toml">
    <img src="https://img.shields.io/badge/python-%3E%3D3.12-brightgreen.svg" alt="Python">
  </a>
  <a href="package.json">
    <img src="https://img.shields.io/badge/node-%3E%3D20-brightgreen.svg" alt="Node">
  </a>
  <a href="#quick-start">
    <img src="https://img.shields.io/badge/llm-claude%20code%20cli-purple.svg" alt="LLM">
  </a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#how-it-works">How It Works</a> •
  <a href="#slash-commands">Slash Commands</a> •
  <a href="#dashboard">Dashboard</a> •
  <a href="#configuration">Configuration</a> •
  <a href="TROUBLESHOOTING.md">Troubleshooting</a> •
  <a href="#license">License</a>
</p>

<p align="center">
  It learns both corrections and successful execution patterns—so Claude Code avoids repeating mistakes and reuses what works. Playbook rules travel with you across every project; per-project profiles capture how you like to work on each repo.
</p>

<p align="center">
  <b>Head-to-head vs <code>claude-mem</code>.</b> Evaluated across 12 scripted scenarios.
  <br>
  <b>~2.7× higher overall quality </b> &nbsp;·&nbsp; Right user correction learnt every time vs none  &nbsp;·&nbsp; See <a href="benchmarks/memory_comparison/EXPERIMENT.md">EXPERIMENT.md</a> for details.
</p>

---

## Why Learning, Not Memory

Most memory solutions are still mostly informative—Claude remembers what happened, without necessarily changing what it does next.

`claude-smart` focuses on learning instead.

Four ways this changes what Claude Code can do for you:

- 💡 **Stop repeating the same mistakes:** Produces actionable playbooks Claude can follow next time; memory only records what happened.

  > *Example:* you tell Claude to stop running `npm test` without `--run` because watch mode hangs.<br>
  > **Memory:** “user was annoyed about npm test hanging”<br>
  > **Learning:** “always pass `--run` to `npm test` in this repo — default watch mode blocks CI”

- 🚀 **Start from the optimized path:** Preserves and optimizes execution paths so Claude can reuse what already works.
  > *Example:* Claude spends several iterations trying to start the local dev environment before discovering that this repo requires `pnpm dev:all` instead of the usual `npm run dev`.<br>
  > **Memory:** “user mentioned that `npm run dev` did not work”<br>
  > **Learning:** “for this repo, always use `pnpm dev:all` to start the full local stack — `npm run dev` only starts the frontend and causes missing service errors”

  Instead of re-exploring, Claude starts from the proven path—reducing planning steps, latency, and token usage.

- 🌐 **Global playbook, project-scoped profiles:** Session memory disappears with the conversation. The playbook persists *globally* — rules learned in one repo are available in every repo you work in — while user profiles stay scoped to the current project so per-repo preferences don't leak across projects.

- 🪶 **Better context without prompt bloat:** Distilled, deduplicated playbooks stay in dozens of tokens—not thousands—even as the project grows.

---

## Quick Start

```bash
npx claude-smart install     # or: uvx claude-smart install
```

Or run the equivalent marketplace commands directly via the Claude Code CLI:

```bash
claude plugin marketplace add ReflexioAI/claude-smart
claude plugin install claude-smart@reflexioai
```

Then restart Claude Code.

To uninstall:

```bash
npx claude-smart uninstall     # or: uvx claude-smart uninstall
```

Or run the equivalent command directly via the Claude Code CLI:

```bash
claude plugin uninstall claude-smart@reflexioai
```

Local data under `~/.reflexio/` and `~/.claude-smart/` is left in place — remove manually if desired.

Developing the plugin itself? See [DEVELOPER.md](./DEVELOPER.md#developing-locally).
> **Not supported:** Claude Code Cowork and claude.ai/code web — they run in a remote sandbox, so the local backend/dashboard and `~/.reflexio/` aren't reachable.

---

## Key Features

- 🧠 **Learn, don't just remember** — Corrections become structured, deduplicated rules, not transcript replays.
- ⚡ **Fully automatic learning** — Every user turn, tool call, and assistant response is captured via lifecycle hooks and extracted into rules without you running anything.
- 📈 **Updates with every session** — Playbooks auto-merge, supersede, and archive as your project evolves — the playbook sharpens with use instead of bloating.
  > *e.g.* you correct the same `npm test --run` gotcha twice → **claude-smart** consolidates them into one stronger rule. Later you switch the policy to `pnpm test` → the old rule is archived and the new one supersedes it, no manual cleanup.
- 🔌 **No external API call** — semantic search runs on an in-process ONNX embedder (all-MiniLM-L6-v2), and all data (profiles, playbooks, interaction buffers) is stored locally on your machine (`~/.reflexio/` and `~/.claude-smart/`).
- 🔎 **Hybrid search** — Playbooks and profiles are indexed with vector + BM25 search for fast, robust retrieval.
- 🧪 **Offline resilience** — If the reflexio backend is down, hooks buffer to disk; the next successful publish drains them.
- 🧰 **Manual correction tag** — `/claude-smart:tag` flags the last turn as a correction so the extractor weights it heavily.

---

## Dashboard

A web UI for browsing session histories, inspecting user profiles, and editing playbook rules. The dashboard auto-starts alongside the backend, so you can open **http://localhost:3001** directly. Or run `/claude-smart:dashboard` in Claude Code to launch dashboard in browser.

<p align="center">
  <img src="assets/profile_dashboard.png" alt="Profile dashboard" width="49%">
  <img src="assets/playbook_dashboard.png" alt="Playbook dashboard" width="49%">
</p>

---

## How It Works

claude-smart builds two artifacts as you work and injects them into Claude at the start of every new session:

- **User profile** (project-scoped) — preferences about how you work in this specific repo (stack, role, small quirks). *e.g.* "uses pnpm, not npm"; "prefers terse answers"; "backend engineer — explain frontend with backend analogues."
- **Playbook** (global, cross-project) — durable, generalized rules accumulated across every session, shared across every project you use claude-smart in. Each rule says when it applies and why, so a rule learned in one repo only fires in the contexts where it's relevant. *e.g.* "always pass `--run` to `npm test` — watch mode hangs CI"; "use real Postgres for integration tests — mocks once hid a broken migration."

Playbooks clean themselves up: correct the same thing twice and they merge; change your mind and the old one is archived.

Under the hood: hooks watch your turns, tool calls, and Claude's replies, auto-flagging corrections (or anything you `/tag`). At session end (or on `/learn`), [reflexio](https://github.com/ReflexioAI/reflexio) — the self-improving engine that powers claude-smart — extracts profile entries and playbook rules. Next session, both get injected into the system prompt — run `/show` to see what Claude is being told. Everything runs on your machine.

**Citations (`cs-cite`).** At the end of a reply, Claude may run:

```
⏺ Bash(cs-cite r1-252,p1-5aed)
  ⎿  (No output)

⏺ ✨ 2 claude-smart learnings applied
```

That signals a profile entry (`p…`) or playbook rule (`r…`) materially shaped the reply. Open the interaction's detail page in the [dashboard](#dashboard) to see the exact cited item.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for hooks, data flow, and reflexio details.

---

## Slash Commands

| Command | What it does |
| --- | --- |
| `/dashboard` | Open the dashboard in your browser, auto-starting the reflexio backend and dashboard services if they aren't already running. |
| `/show` | Print the current (global) playbook plus the current project's user profiles (same markdown that `SessionStart` injects). Use it to audit what playbooks and preferences Claude is being told to follow. |
| `/learn` | Force reflexio to run extraction *now* on the current session's unpublished interactions. Without this, extraction runs at the end of the session or on reflexio's batch interval. |
| `/tag [note]` | Tag the most recent turn as a correction, for cases the automatic heuristic missed. The note becomes the correction description the extractor sees. |
| `/restart` | Restart the reflexio backend and dashboard to pick up new changes (e.g. after upgrading the plugin or editing local reflexio code). |
| `/clear-all` | **Destructive.** Delete *all* reflexio interactions, profiles, and user playbooks. Use when you want to wipe learned state and start fresh. |

---

## Configuration

Advanced users can tune claude-smart via environment variables — see [DEVELOPER.md](./DEVELOPER.md#environment-variables) for the full list.

### Where data lives

| Path | What |
| --- | --- |
| `~/.reflexio/data/reflexio.db` | Source of truth — profiles, user_playbooks, interactions, FTS5 indexes, and vec0 embedding tables (plus `.db-shm` / `.db-wal` WAL sidecars). Inspect with `sqlite3`. |
| `~/.reflexio/.env` | Provider config — `CLAUDE_SMART_USE_LOCAL_CLI`, `CLAUDE_SMART_USE_LOCAL_EMBEDDING`, any optional API keys. |
| `~/.reflexio/plugin-root` | Self-healed symlink to the active plugin dir (managed by `ensure-plugin-root.sh` — written on install, refreshed each `SessionStart`). Slash commands resolve through it, so don't delete it; if you do, the next session will recreate it. |
| `~/.claude-smart/sessions/{session_id}.jsonl` | Per-session buffer. User turns, assistant turns, tool invocations, `{"published_up_to": N}` watermarks. Safe to inspect and safe to delete — everything past the latest watermark has already been written to reflexio's DB. |
| `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/` | Cached ONNX weights (~86 MB, downloaded once). Delete to force a re-download. |

For troubleshooting, see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

## License

This project is licensed under the **Apache License 2.0**. The bundled `reflexio/` submodule is also Apache 2.0. Claude Code is Anthropic's and not covered by this license.

See the [LICENSE](LICENSE) file for details.

---

## Support

- **Issues**: open one on GitHub describing the symptom and include the reflexio backend log (`~/.claude-smart/backend.log`) and the relevant lines of `~/.claude-smart/sessions/{session_id}.jsonl`.

---

**Built on** [reflexio](https://github.com/ReflexioAI/reflexio) · **Runs on** [Claude Code](https://claude.com/claude-code) · **Written in** Python 3.12+
