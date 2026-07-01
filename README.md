<p align="center">
  <img src="assets/claude-smart-icon.png" alt="claude-smart" width="140">
</p>

<h1 align="center">
  claude-smart
</h1>

<h4 align="center">A self-improvement plugin for <a href="https://claude.com/claude-code" target="_blank">Claude Code</a>, Codex, and OpenCode that turns interactions into durable skills they follow in future sessions.</h4>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  </a>
  <a href="plugin/pyproject.toml">
    <img src="https://img.shields.io/badge/version-0.2.46-green.svg" alt="Version">
  </a>
  <a href="plugin/pyproject.toml">
    <img src="https://img.shields.io/badge/python-%3E%3D3.12-brightgreen.svg" alt="Python">
  </a>
  <a href="package.json">
    <img src="https://img.shields.io/badge/node-%3E%3D20-brightgreen.svg" alt="Node">
  </a>
  <a href="#quick-start">
    <img src="https://img.shields.io/badge/hosts-Claude%20Code%20%2B%20Codex%20%2B%20OpenCode-purple.svg" alt="Hosts">
  </a>
  <a href="https://discord.gg/7fnCxahase">
    <img src="https://img.shields.io/badge/Discord-Join%20community-5865F2?logo=discord&logoColor=white" alt="Discord">
  </a>
  <a href="https://github.com/ReflexioAI/reflexio">
    <img src="https://img.shields.io/github/stars/ReflexioAI/reflexio?style=flat&logo=github&label=Star%20reflexio&color=yellow" alt="Star reflexio on GitHub">
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
  claude-smart learns from your corrections <i>and</i> from what's already working — so the assistant stops repeating mistakes and reuses proven paths. Project-specific skills capture repo-local rules, and shared skills roll up durable patterns for reuse across projects.
</p>

<p align="center">
  <b>vs <code>claude-mem</code>:</b> ~3× better at turning your corrections into rules Claude follows, ~50% more of your guidance is retained. <a href="EXPERIMENT.md"><b>Read the benchmark →</b></a>
</p>

<p align="center">
  ⭐ Built on the open-source <a href="https://github.com/ReflexioAI/reflexio"><b>reflexio</b></a> engine — if claude-smart helps you, <a href="https://github.com/ReflexioAI/reflexio"><b>star reflexio on GitHub</b></a>.
</p>

---

## Quick Start

### Claude Code

```bash
npx claude-smart install     # or: uvx claude-smart install
```

Then restart Claude Code.

Requires Node.js (for `npx`) or uv (for `uvx`) to already exist.

Alternatively, install via Claude Code's plugin marketplace:

```bash
claude plugin marketplace add ReflexioAI/claude-smart
claude plugin install claude-smart@reflexioai
```

To uninstall:

```bash
npx claude-smart uninstall     # or: uvx claude-smart uninstall
```

Or, if installed via the plugin marketplace:

```bash
claude plugin uninstall claude-smart@reflexioai
```

### Codex

```bash
npx claude-smart install --host codex
```

Then fully quit and reopen Codex so hooks reload.

Requires the `codex` CLI on `PATH` and Node.js (for `npx`).

To uninstall:

```bash
npx claude-smart uninstall --host codex
```

Restart Codex after uninstalling. The uninstaller stops local claude-smart services and removes plugin/cache/config state; learned data under `~/.reflexio/` and `~/.claude-smart/` is preserved and shared with Claude Code, so you can switch between hosts without losing skills or preferences.

### OpenCode

```bash
npx claude-smart install --host opencode
```

Then restart OpenCode in your project so it loads the plugin from `opencode.json`, the documented project config file. If that project does not already have a root config but does have `.opencode/opencode.json` or `.opencode/opencode.jsonc`, the installer updates that existing file instead of creating a second config. If both locations exist, the root config wins. Use `--global` to install into `~/.config/opencode/opencode.json` for all OpenCode projects on this machine. The installer copies the active package to `~/.claude-smart/opencode/claude-smart`, prepares that runtime immediately, and writes a `file://` plugin entry so OpenCode reloads the same prepared package after restart.

OpenCode support is new and uses OpenCode's plugin loader to inject relevant learned context before each model request. Learning extraction runs `opencode run --pure` from an isolated temp project, so it uses OpenCode's default model unless you set `CLAUDE_SMART_OPENCODE_MODEL=provider/model`. Set that env var if your normal project config pins a different provider or model.

To uninstall:

```bash
npx claude-smart uninstall --host opencode
```

Restart OpenCode after uninstalling. The uninstaller removes the `claude-smart` entry from OpenCode's plugin list and deletes the copied OpenCode package; learned data under `~/.reflexio/` and `~/.claude-smart/` is preserved and shared across hosts.

Developing the plugin itself? See [DEVELOPER.md](./DEVELOPER.md#developing-locally) for what the installer does, manual toggles via `/plugins`, and clone-based development.

> **Not supported:** Claude Code Cowork, claude.ai/code web, or remote Codex environments without local plugin hooks — they run outside your local machine, so the local backend/dashboard and `~/.reflexio/` aren't reachable.

---

## Why Learning, Not Memory

Most memory tools just record. Claude remembers what happened, but doesn't change what it does next.

`claude-smart` focuses on learning instead.

Four things this changes:

- 💡 **Stop repeating the same mistakes:** Produces actionable skills Claude can follow next time; memory only records what happened.

  > *Example:* a deploy fails after Claude bumps `prisma` from 5.x to 6.0; you tell it to roll back because 6.0 breaks nested writes in your order flow.<br>
  > **Memory:** “deploy broke after prisma bump; user rolled back”<br>
  > **Learning:** “treat major-version bumps of ORMs/DB drivers as breaking — verify with integration tests, not just unit tests”

- 🚀 **Start from the optimized path:** Records and optimizes the steps that worked so Claude can reuse them, instead of re-exploring each session.
  > *Example:* Claude spends several iterations trying to start the local dev environment before discovering that this repo requires `pnpm dev:all` instead of the usual `npm run dev`.<br>
  > **Memory:** “user mentioned that `npm run dev` did not work”<br>
  > **Learning:** “for this repo, always use `pnpm dev:all` to start the full local stack — `npm run dev` only starts the frontend and causes missing service errors”

  Instead of re-exploring, Claude Code starts from the proven path—reducing planning steps, latency, and token usage.

- 🌐 **Project-specific and shared skills:** Session memory disappears with the conversation. Project-specific skills preserve repo-local rules, while shared skills roll up patterns that should transfer across projects. Preferences stay scoped to the current project so per-repo preferences don't leak across projects.

- 🪶 **Better context without prompt bloat:** Distilled, deduplicated skills stay in dozens of tokens—not thousands—even as the project grows.

---

## Demo

https://github.com/user-attachments/assets/ed84d73a-d5b7-4a0d-ab2c-f2c060c3f276

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

A web UI for browsing session histories, inspecting preferences, and editing project-specific and shared skills. The dashboard auto-starts alongside the backend, so you can open **http://localhost:3001** directly. Or run `/claude-smart:dashboard` in Claude Code to open it in your browser. In Codex, run `bash ~/.reflexio/plugin-root/scripts/dashboard-open.sh`.

<p align="center">
  <img src="assets/preferences_dashboard.png" alt="Preferences dashboard" width="49%">
  <img src="assets/skills_dashboard.png" alt="Skills dashboard" width="49%">
</p>

---

## How It Works

claude-smart builds three artifacts as you work and injects the relevant ones into Claude Code, Codex, or OpenCode:

- **Preferences** (project-scoped) — how you work in this specific repo (stack, role, small quirks). *e.g.* "uses pnpm, not npm"; "prefers terse answers"; "backend engineer — explain frontend with backend analogues."
- **Project-specific skills** — durable rules with triggers and rationales learned from corrections in a project. *e.g.* "always pass `--run` to `npm test` — watch mode hangs CI."
- **Shared skills** — optimized rollups of project-specific skills that should transfer across projects. *e.g.* "use real Postgres for integration tests — mocks once hid a broken migration."

Skills clean themselves up: correct the same thing twice and they merge; change your mind and the old one is archived.

Under the hood: hooks watch your turns, tool calls, and assistant replies, auto-flagging corrections (or anything you flag with `/claude-smart:learn`). At session end (or on `/claude-smart:learn`), [reflexio](https://github.com/ReflexioAI/reflexio) — the self-improving engine that powers claude-smart — extracts preferences and project-specific skills, then rolls durable patterns into shared skills. On each new user prompt, claude-smart searches for matching context and injects only the relevant hits. Run `/claude-smart:show` or the equivalent CLI command to audit the current learned state. Everything runs on your machine.

**Citations.** At the end of a reply, the assistant may append a short marker:

```
✨ claude-smart rule applied: [git safety](http://localhost:3001/rules/s1-123) | [brief answer preference](http://localhost:3001/rules/p1-pref) · [⚡Reflexio](https://github.com/ReflexioAI/reflexio)
```

That signals a preference or skill materially shaped the reply.
Open the session's detail page in the [dashboard](http://localhost:3001) to see the exact cited item.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for hooks, data flow, and reflexio details.

---

## Commands

Claude Code installs these as plugin slash commands. Codex does not currently
support plugin-provided slash commands, so claude-smart ships a Codex skill that
maps requests like "claude-smart show" or "run claude-smart learn" to the
equivalent action.

| Claude Code | Codex request | What it does |
| --- | --- | --- |
| `/claude-smart:dashboard` | `open claude-smart dashboard` | Open the dashboard in your browser, auto-starting the reflexio backend and dashboard services if they aren't already running. |
| `/claude-smart:show` | `claude-smart show` | Print current project-specific skills, shared skills, and the current project's preferences so you can audit learned state manually. |
| `/claude-smart:learn [note]` | `claude-smart learn with note "optional note"` | Flag the most recent turn as a correction (for cases the automatic heuristic missed) and force reflexio to run extraction *now* on the session's unpublished interactions. The optional note becomes the correction description the extractor sees. |
| `/claude-smart:restart` | `restart claude-smart` | Restart the reflexio backend and dashboard to pick up new changes (e.g. after upgrading the plugin or editing local reflexio code). |
| `/claude-smart:clear-all` | `clear all claude-smart learnings` | **Destructive.** Delete *all* reflexio interactions, preferences, and skills. Use when you want to wipe learned state and start fresh. |

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
| `opencode.json` / `opencode.jsonc`, or existing `.opencode/opencode.json*` | OpenCode local plugin config patched by `claude-smart install --host opencode`; the installer updates only the claude-smart entry in OpenCode's plugin list. |
| `~/.claude-smart/opencode/claude-smart/` | OpenCode's stable local copy of the active npm package. OpenCode config points here with `file://` so restarts do not re-resolve `claude-smart` from npm. |
| `~/.reflexio/plugin-root` | Self-healed symlink to the active plugin dir (managed by `ensure-plugin-root.sh` — written on install, refreshed each `SessionStart`). Claude Code slash commands and Codex shell-command helpers resolve through it, so don't delete it; if you do, the next session will recreate it. |
| `~/.claude-smart/sessions/{session_id}.jsonl` | Per-session buffer. User turns, assistant turns, tool invocations, `{"published_up_to": N}` watermarks. Safe to inspect and safe to delete — everything past the latest watermark has already been written to reflexio's DB. |
| `~/.claude-smart/node/current/` | Private Node.js/npm runtime used by hooks and the dashboard after install. |
| `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/` | Cached ONNX weights (~86 MB, downloaded once). Delete to force a re-download. |

For troubleshooting, see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

## Powered by Reflexio

claude-smart is powered by [**Reflexio**](https://github.com/ReflexioAI/reflexio) ([reflexio.ai](https://reflexio.ai)) — the open-source self-improving engine that turns interactions into durable, reusable skills. If claude-smart is useful to you, **[⭐ star reflexio on GitHub](https://github.com/ReflexioAI/reflexio)**.

---

## License

This project is licensed under the **Apache License 2.0**. Reflexio is a separate Apache 2.0 project consumed as the `reflexio-ai` Python package. Claude Code is Anthropic's and not covered by this license.

See the [LICENSE](LICENSE) file for details.

---

## Support

- **Discord**: join the community at [discord.gg/7fnCxahase](https://discord.gg/7fnCxahase).
- **Issues**: open one on GitHub describing the symptom and include the reflexio backend log (`~/.claude-smart/backend.log`) and the relevant lines of `~/.claude-smart/sessions/{session_id}.jsonl`.

---

**Powered by** [Reflexio](https://github.com/ReflexioAI/reflexio) · **Runs on** [Claude Code](https://claude.com/claude-code), Codex, and OpenCode · **Written in** Python 3.12+
