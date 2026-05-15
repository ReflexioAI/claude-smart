<p align="center">
  <img src="assets/claude-smart-icon.png" alt="claude-smart" width="140">
</p>

<h1 align="center">
  claude-smart
</h1>

<h4 align="center">A local learning plugin for <a href="https://claude.com/claude-code" target="_blank">Claude Code</a> and Codex that turns corrections into durable rules your coding assistant follows in future sessions.</h4>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  </a>
  <a href="plugin/pyproject.toml">
    <img src="https://img.shields.io/badge/version-0.2.24-green.svg" alt="Version">
  </a>
  <a href="plugin/pyproject.toml">
    <img src="https://img.shields.io/badge/python-%3E%3D3.12-brightgreen.svg" alt="Python">
  </a>
  <a href="package.json">
    <img src="https://img.shields.io/badge/node-%3E%3D20-brightgreen.svg" alt="Node">
  </a>
  <a href="#quick-start">
    <img src="https://img.shields.io/badge/hosts-Claude%20Code%20%2B%20Codex-purple.svg" alt="Hosts">
  </a>
  <a href="https://discord.gg/Jbft3jPn">
    <img src="https://img.shields.io/badge/Discord-Join%20Chat-5865F2?logo=discord&logoColor=white" alt="Discord">
  </a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#how-it-works">How It Works</a> •
  <a href="#commands">Commands</a> •
  <a href="#dashboard">Dashboard</a> •
  <a href="#configuration">Configuration</a> •
  <a href="TROUBLESHOOTING.md">Troubleshooting</a> •
  <a href="#license">License</a>
</p>

<p align="center">
  It learns both corrections and successful execution patterns—so your coding assistant avoids repeating mistakes and reuses what works. Project-specific skills capture repo-local rules, and shared skills roll up durable patterns for reuse across projects.
</p>

<p align="center">
  <b>vs <code>claude-mem</code>:</b> ~3× better at turning your corrections into rules Claude follows, ~50% more of what you tell Claude sticks. <a href="EXPERIMENT.md"><b>Read the benchmark →</b></a>
</p>

---

## Why Learning, Not Memory

Most memory solutions are still mostly informative—Claude remembers what happened, without necessarily changing what it does next.

`claude-smart` focuses on learning instead.

Four ways this changes what your coding assistant can do for you:

- 💡 **Stop repeating the same mistakes:** Produces actionable skills Claude can follow next time; memory only records what happened.

  > *Example:* a deploy fails after Claude bumps `prisma` from 5.x to 6.0; you tell it to roll back because 6.0 breaks nested writes in your order flow.<br>
  > **Memory:** “deploy broke after prisma bump; user rolled back”<br>
  > **Learning:** “treat major-version bumps of ORMs/DB drivers as breaking — verify with integration tests, not just unit tests”

- 🚀 **Start from the optimized path:** Preserves and optimizes execution paths so Claude can reuse what already works.
  > *Example:* Claude spends several iterations trying to start the local dev environment before discovering that this repo requires `pnpm dev:all` instead of the usual `npm run dev`.<br>
  > **Memory:** “user mentioned that `npm run dev` did not work”<br>
  > **Learning:** “for this repo, always use `pnpm dev:all` to start the full local stack — `npm run dev` only starts the frontend and causes missing service errors”

  Instead of re-exploring, Claude starts from the proven path—reducing planning steps, latency, and token usage.

- 🌐 **Project-specific and shared skills:** Session memory disappears with the conversation. Project-specific skills preserve repo-local rules, while shared skills roll up patterns that should transfer across projects. Preferences stay scoped to the current project so per-repo preferences don't leak across projects.

- 🪶 **Better context without prompt bloat:** Distilled, deduplicated skills stay in dozens of tokens—not thousands—even as the project grows.

---

## Quick Start

### Claude Code

```bash
claude plugin marketplace add ReflexioAI/claude-smart
claude plugin install claude-smart@reflexioai
```

The plugin Setup hook installs its own `uv`, Python 3.12 environment, and
private Node.js/npm runtime under `~/.claude-smart/` when they are missing, so
you do not need to install Python or Node globally for the plugin/dashboard.

If you already have Node.js or uv, these convenience wrappers are equivalent
but require their own runtime to already exist:

```bash
npx claude-smart install     # or: uvx claude-smart install
```

Then restart Claude Code.

To uninstall:

```bash
claude plugin uninstall claude-smart@reflexioai
```

Or, if you already have Node.js or uv:

```bash
npx claude-smart uninstall     # or: uvx claude-smart uninstall
```

### Codex

You need the `codex` CLI on `PATH` and Node.js available for `npx`:

```bash
npx claude-smart install --host codex
```

The helper registers the bundled **ReflexioAI** marketplace with Codex, enables
Codex's `hooks` and `plugin_hooks` features, installs `claude-smart` into
Codex's local plugin cache, enables it in `~/.codex/config.toml`, and seeds
Codex's per-hook trust state for the claude-smart hooks. `codex features enable
plugin_hooks` alone is not enough; it only turns on the global plugin-hook
feature and does not install the plugin or trust the individual hook entries.
If you need one command that prepares everything, rerun:

```bash
npx claude-smart install --host codex
```

Hooks are not active in already-running Codex windows; after the command finishes:

1. Fully quit and reopen Codex in your project so hooks reload.
2. Run `/plugins` only if you want to verify `claude-smart` shows as installed
   from the **ReflexioAI** marketplace.

If you install or toggle `claude-smart` manually from `/plugins`, still run
`npx claude-smart install --host codex` once afterward so the hook feature
flags, plugin cache, and claude-smart hook trust state are prepared.

Do not create a `~/plugins/claude-smart` symlink for a normal `npx` install;
that symlink is only for plugin development from a cloned checkout.

Installing from a clone is only for plugin development; see
[DEVELOPER.md](./DEVELOPER.md#developing-locally).

Codex and Claude Code intentionally share the same `CLAUDE_SMART_*` environment
variables, `~/.reflexio/` data, `~/.claude-smart/` session buffers, backend,
dashboard, and learned skills/preferences.


To uninstall:

```bash
npx claude-smart uninstall --host codex
```

This removes the Codex marketplace registration, installed plugin config, and
Codex plugin cache. Local data under `~/.reflexio/` and `~/.claude-smart/` is
left in place — remove manually if desired. Restart Codex after uninstalling.

Developing the plugin itself? See [DEVELOPER.md](./DEVELOPER.md#developing-locally).
> **Not supported:** Claude Code Cowork, claude.ai/code web, or remote Codex environments without local plugin hooks — they run outside your local machine, so the local backend/dashboard and `~/.reflexio/` aren't reachable.

---

## Key Features

- 🧠 **Learn, don't just remember** — Corrections become structured, deduplicated rules, not transcript replays.
- ⚡ **Fully automatic learning** — Every user turn, tool call, and assistant response is captured via lifecycle hooks and extracted into rules without you running anything.
- 📈 **Continuously self-tuning, not just append-on-conflict** — Existing rules are continuously refined, not just appended to. Wording gets clearer, *when-to-apply* triggers tighten or broaden as evidence accumulates, near-duplicates merge, stale rules are superseded, and dead ones are archived. The library gets sharper, not just bigger.
  > *e.g.* correct the same `npm test --run` gotcha twice → consolidated into one rule. New evidence shows it applies to `vitest` too → scope broadened. Switch policy to `pnpm test` → old rule archived, new one supersedes it.
- 🔌 **No external API call** — semantic search runs on an in-process ONNX embedder (all-MiniLM-L6-v2), and all data (preferences, skills, interaction buffers) is stored locally on your machine (`~/.reflexio/` and `~/.claude-smart/`).
- 🔎 **Hybrid search** — Skills and preferences are indexed with vector + BM25 search for fast, robust retrieval.
- 🧪 **Offline resilience** — If the reflexio backend is down, hooks buffer to disk; the next successful publish drains them.
- 🧰 **Manual correction marker** — `/claude-smart:learn` in Claude Code, or `bash ~/.reflexio/plugin-root/scripts/cli.sh learn` in Codex, flags the last turn as a correction so the extractor weights it heavily.

---

## Dashboard

A web UI for browsing session histories, inspecting preferences, and editing project-specific and shared skills. The dashboard auto-starts alongside the backend, so you can open **http://localhost:3001** directly. Or run `/claude-smart:dashboard` in Claude Code to launch dashboard in browser. In Codex, run `bash ~/.reflexio/plugin-root/scripts/dashboard-open.sh`.

<p align="center">
  <img src="assets/preferences_dashboard.png" alt="Preferences dashboard" width="49%">
  <img src="assets/skills_dashboard.png" alt="Skills dashboard" width="49%">
</p>

---

## How It Works

claude-smart builds three artifacts as you work and injects the relevant ones into Claude Code or Codex:

- **Preferences** (project-scoped) — how you work in this specific repo (stack, role, small quirks). *e.g.* "uses pnpm, not npm"; "prefers terse answers"; "backend engineer — explain frontend with backend analogues."
- **Project-specific skills** — durable rules with triggers and rationales learned from corrections in a project. *e.g.* "always pass `--run` to `npm test` — watch mode hangs CI."
- **Shared skills** — optimized rollups of project-specific skills that should transfer across projects. *e.g.* "use real Postgres for integration tests — mocks once hid a broken migration."

Skills clean themselves up: correct the same thing twice and they merge; change your mind and the old one is archived.

Under the hood: hooks watch your turns, tool calls, and assistant replies, auto-flagging corrections (or anything you flag with `/claude-smart:learn`). At session end (or on `/claude-smart:learn`), [reflexio](https://github.com/ReflexioAI/reflexio) — the self-improving engine that powers claude-smart — extracts preferences and project-specific skills, then rolls durable patterns into shared skills. On each new user prompt, claude-smart searches for matching context and injects only the relevant hits. Run `/claude-smart:show` or the equivalent CLI command to audit the current learned state. Everything runs on your machine.

**Citations.** At the end of a reply, the assistant may append a short marker:

```
✨ 2 claude-smart learnings applied [cs:s1-252,p1-5aed]
```

That signals a preference (`p…`) or skill (`s…`) materially shaped the reply.
Standalone wrappers like `✨abc123✨` are not claude-smart citations and will not
link back to dashboard entries. Open the interaction's detail page in the
[dashboard](#dashboard) to see the exact cited item.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for hooks, data flow, and reflexio details.

---

## Commands

Claude Code installs these as plugin slash commands. Codex does not currently
support plugin-provided slash commands, so claude-smart ships a Codex skill that
maps requests like "claude-smart show" or "run claude-smart learn" to the
equivalent shell command. You can also run the shell command directly.

| Claude Code | Codex request | Direct shell fallback | What it does |
| --- | --- | --- | --- |
| `/claude-smart:dashboard` | `open claude-smart dashboard` | `bash ~/.reflexio/plugin-root/scripts/dashboard-open.sh` | Open the dashboard in your browser, auto-starting the reflexio backend and dashboard services if they aren't already running. |
| `/claude-smart:show` | `claude-smart show` | `bash ~/.reflexio/plugin-root/scripts/cli.sh show` | Print current project-specific skills, shared skills, and the current project's preferences so you can audit learned state manually. |
| `/claude-smart:learn [note]` | `claude-smart learn with note "optional note"` | `bash ~/.reflexio/plugin-root/scripts/cli.sh learn --note "optional note"` | Flag the most recent turn as a correction (for cases the automatic heuristic missed) and force reflexio to run extraction *now* on the session's unpublished interactions. The optional note becomes the correction description the extractor sees. |
| `/claude-smart:restart` | `restart claude-smart` | `bash ~/.reflexio/plugin-root/scripts/cli.sh restart` | Restart the reflexio backend and dashboard to pick up new changes (e.g. after upgrading the plugin or editing local reflexio code). |
| `/claude-smart:clear-all` | `clear all claude-smart learnings` | `bash ~/.reflexio/plugin-root/scripts/cli.sh clear-all --yes` | **Destructive.** Delete *all* reflexio interactions, preferences, and skills. Use when you want to wipe learned state and start fresh. |

---

## Configuration

Advanced users can tune claude-smart via environment variables — see [DEVELOPER.md](./DEVELOPER.md#environment-variables) for the full list.

### Where data lives

| Path | What |
| --- | --- |
| `~/.reflexio/data/reflexio.db` | Source of truth for learned preferences, skills, interactions, full-text indexes, and embedding tables (plus `.db-shm` / `.db-wal` WAL sidecars). Inspect with `sqlite3`. |
| `~/.reflexio/.env` | Provider config — `CLAUDE_SMART_USE_LOCAL_CLI`, `CLAUDE_SMART_USE_LOCAL_EMBEDDING`, any optional API keys. |
| `.claude/settings.local.json` or `~/.claude/settings.json` | Claude Code hook environment, such as `CLAUDE_SMART_ENABLE_OPTIMIZER`; use project-local settings for one repo or user settings for all projects. |
| `~/.codex/config.toml` | Codex plugin state, hook feature flags, and per-hook trust entries after `claude-smart install --host codex`. |
| `~/.codex/plugins/cache/reflexioai/claude-smart/<version>/` | Codex's cached install of the `claude-smart` plugin from the `ReflexioAI` marketplace. |
| `~/.reflexio/plugin-root` | Self-healed symlink to the active plugin dir (managed by `ensure-plugin-root.sh` — written on install, refreshed each `SessionStart`). Claude Code slash commands and Codex shell-command helpers resolve through it, so don't delete it; if you do, the next session will recreate it. |
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

**Built on** [reflexio](https://github.com/ReflexioAI/reflexio) · **Runs on** [Claude Code](https://claude.com/claude-code) and Codex · **Written in** Python 3.12+
