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
    <img src="https://img.shields.io/badge/version-0.3.17-green.svg" alt="Version">
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
    <img src="https://img.shields.io/github/stars/ReflexioAI/reflexio?style=flat&logo=github&label=Star%20reflexio&color=yellow&cacheSeconds=3600" alt="Star reflexio on GitHub">
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

Requires Node.js 20+ (for `npx`) and the host CLI you're installing into (`claude`, `codex`, or `opencode`) on `PATH`.

### Guided installation

```bash
npx claude-smart install
```

Choose one or more detected coding agents with the arrow keys and Space, then
press Enter. Single-choice settings use the arrow keys and Enter. Esc or Ctrl+C
cancels before configuration is saved.

The guide asks where to store and process your data:

- **Local Reflexio:** starts a background backend on this machine. Choose which
  available agent CLI powers background learning; it can analyze interactions
  from every installed integration.
- **Managed Reflexio:** enter an HTTPS endpoint and a hidden API key. No local
  backend starts; memory storage and background learning run in Reflexio cloud.
  The local dashboard connects to that cloud backend by default.

In local mode, Claude Code learning defaults to the `sonnet` alias, which Claude
Code resolves independently of the coding model. Codex and OpenCode keep their
provider-configured defaults. An explicit supported model ID overrides these
choices. For unattended installs, use
`--model <model-id>` with `--provider`. The chosen model is applied to extraction
and optimizer calls. Managed mode reports the server-configured model when
available, otherwise explicitly reports that its default model is not exposed.
After install, update, or setup, the summary includes the Claude-smart version,
learning provider, provider CLI version when available, and model selection.
Configured aliases/defaults are distinguished from a reported model version;
unknown versions are shown as “not reported.” Run `npx claude-smart setup` to
change the learning settings.
When local learning is already configured, **Keep current learning configuration**
is selected by default. It preserves cloud generation, custom executables and the
current native model; selecting a native provider entry explicitly changes that
setup and opens its model choices. Supported caller overrides still apply, and
`--provider` explicitly replaces a cloud/custom configuration with that native
provider. Installing a different coding integration does not select its model.

The dashboard's **Data source** dropdown selects Local or Managed Reflexio.
It defaults to the saved runtime mode. Its endpoint, overview totals, preferences,
skills, and backend sessions follow the selected source; viewing another source
does not change learning/storage settings. The header shows the selected endpoint
as a read-only connection label, with an **Open cloud dashboard** link in Managed
mode. Change connection settings in **Configure**. Overview metrics use backend summary
counts for the last 30 days, rather than list limits. Connection failures are
shown explicitly. Older skill citations without a project/shared identity remain
visible under Legacy skill usage; exact per-skill counts use identified citations.
A backend must be configured and reachable to view its data.
The dashboard follows the managed Reflexio workspace style: neutral light/dark
surfaces, green accents, flat metric bands, and consistent page spacing. The
theme switch applies across dashboard, sessions, preferences, skills, and settings.
Previously configured connections are retained privately when changing modes.
The dashboard currently trusts local OS users: other accounts on a shared machine
can access its configured backend connections. Per-user dashboard authentication
requires a separate change.

**This device** lists local session buffers and hook activity separately. Those
counts can differ between machines connected to the same managed account.

Review the configuration before installation starts. Saved settings and installed
agents are preselected on subsequent runs. Preparation and service startup show
phase and elapsed progress; detailed logs live in `~/.claude-smart/install-logs/`.
If an integration fails, the others still install and the summary reports each result.
Runtime environments remain separate so an agent's rollback cannot break another.

### Explicit installation

For an unattended first install, specify the backend. Multiple agents can be
installed in one command:

```bash
npx claude-smart install --host claude-code,codex --backend local --provider codex
npx claude-smart install --host opencode --backend local --provider opencode --global
```

For managed mode, supply the API key through a named environment variable rather
than a command-line argument:

```bash
npx claude-smart install --host claude-code,codex --backend managed \
  --url https://www.reflexio.ai/ --api-key-env REFLEXIO_API_KEY
```

Existing `--host codex` and `--host opencode` commands still work. Without a terminal,
saved backend/provider settings are reused; a fresh install requires `--backend`.
Use `--yes` to skip interactive questions when all required settings are supplied
or saved. For multiple local agents, specify `--provider` unless one is already saved.
Use `--read-only` to retrieve memories without publishing interactions, or `--learn`
to resume learning. The interactive guide offers both modes for either backend.

Setup reads legacy duplicate settings with the same field rules as the runtime:
shared connection/learning fields use the last assignment (including empty), while
caller-overridable execution flags use the first nonempty saved assignment.
Legacy connection fallback treats the endpoint and API key as a pair. If the
current settings contain either assignment, even empty, setup does not borrow
the other from `~/.reflexio/.env`. An incomplete managed connection needs its own
key. Legacy read-only and other runtime preferences still migrate independently.
Setup, Codex hooks, the dashboard, and the shell/Python readers all accept plain
assignments and `export` followed by spaces or tabs.
Explicit caller and setup choices retain their existing precedence. Reading or
cancelling setup does not rewrite the file or prepare a settings publisher.
Legacy migration preserves the selected original value text, including Windows
path quoting; only the assignment prefix is normalized.

Confirmation uses the same native lock and private atomic settings owner as the
dashboard and dependency repair. One captured preview drives the save; concurrent
edits require a fresh preview. Setup prepares a small temporary Python writer from
locked dependencies before changing settings. A preparation failure leaves settings
unchanged. Confirmed settings remain available if a later integration install fails.
Publisher child processes, including UV installation, have a two-minute deadline.
On timeout, setup stops their owned process trees.
If publication cannot be confirmed, setup stops before installing integrations;
reload settings before retrying.

Migration completion is recorded as a fixed first-line comment in that same atomic
settings write, after an optional UTF-8 BOM. Matching text inside a value is not
migration metadata. Existing
migration sidecars are still recognized without rewriting them. Later setup runs
do not reimport deleted legacy preferences after migration has completed.

The confirmed guide owns connection, learning, sharing, and read-only choices.
Host workers consume those saved choices; installing or removing another agent
does not rewrite a shared learning host. Each prepared runtime uses the same
Python repair owner to supply only missing local defaults, with native locking
and private atomic publication. Existing values, including zero and explicit
empty values, are preserved. Ordinary standalone self-repair remains supported.

When a guide installs multiple integrations together, it prepares one account
runtime and saves confirmed settings through that runtime's Python interpreter.
The selected integrations register against it without rebuilding Python or
the dashboard. A registration can be retried after settings were saved.
If setup stops after selecting a prepared runtime, the next setup resumes
starting its services before preparing another replacement. Setup retains that
startup record until the configured local services answer their readiness probes,
so an unsuccessful replacement cannot lose the current startup work. The default
Codex home, another recorded Codex home, or an OpenCode project counts as installed
while its enabled plugin or loader points to a usable runtime. Hooks and commands
use the prepared Python environment. Restart uses the prepared dashboard build.
Rerun `npx claude-smart setup` to repair the environment or dashboard build.

Dashboard saves use the prepared Python settings owner too. They share the
repair lock and publish privately with one atomic file replacement. An explicit
empty value stays present; omitted settings stay unchanged. If relevant settings
change while a save is being prepared, reload and retry rather than overwriting
the newer choices. A busy repair also leaves the file unchanged. Saving requires
the dashboard's prepared sibling `.venv`; run `npx claude-smart update` if that
runtime needs repair. Existing unrelated assignments and comments are retained.
Literal values use quotes only when needed to preserve the same value across
the readers; backslashes are never rewritten as escapes. Expansion, line/control
separators and quote combinations without a shared literal form are refused
before publication.

Legacy saved HOST fallback is preserved as explicit learning intent at confirmation,
also when switching to managed storage with a native optimizer. Existing HOST
text remains available to the pinned SDK; this does not retire its compatibility
input or grant a new optimizer capability. A private worker without confirmed
connection settings refuses with the public setup command. Confirmation saves a
validated local endpoint in its normalized loopback spelling, so accepted numeric
or uppercase hostnames retain local mode. Reading an existing file does not
normalize it. Uninstall still completes with broken settings and skips speculative
shared-service refresh.

### Activate each integration

Run `/reload-plugins` in your existing Claude Code session, or use the update in your next session.
Start a new Codex session to load the update; you can finish your existing session first. If hook trust needs review, use `/hooks`.
Then restart OpenCode in your project so it loads the plugin. The installer writes a loader into the selected scope's `plugins` directory — the project's `.opencode/plugins/` for a project install — and both OpenCode lines load the plugin from there: OpenCode 1.x auto-loads the plugins directory, and OpenCode V2 rejects the old `file://` config-entry shape. The installer removes legacy registry entries for `claude-smart` and entries that point to its account-managed package. Custom file references are preserved. `--global` installs for all OpenCode projects on this machine instead, writing the loader under your user config directory. Scope files must be regular local files in directories without filesystem redirects. On POSIX, loader creation and removal need a protected directory on the project's volume. The installer uses the account directory when it shares that volume, or an owned directory whose ancestors protect it from unrelated writers. If the loader or config changes during installation, resolve the conflict and rerun setup.

Install and update use the same tracked service lifecycle for every coding agent.
Validation of the platform, settings, and OpenCode loader/config happens before
services stop. If preparation fails before commit, the previous packages and
install metadata are restored before restarting only services that were running.
If an installed service reports that stopping failed, installation aborts before
replacing its runtime. Resolve the shutdown failure and retry; only services
whose stop succeeded are eligible for automatic restart.
A failed first install leaves no candidate runtime selected. Incomplete recovery
keeps backups and warns rather than starting an unverified runtime.

Codex stages its marketplace under `CODEX_HOME/claude-smart/marketplace` (default
`~/.codex/claude-smart/marketplace`), separate from Claude's legacy marketplace.
Installing or removing Codex leaves that legacy runtime intact. Codex writes
the commands for its prepared runtime to the marketplace and cache manifests.
New Codex hooks use a pinned Node executable so replacing the installer's
current Node does not interrupt them.
Guided setup records the selected runtime under the claude-smart account. Current
commands use that selection even if a running legacy hook rewrites
`~/.reflexio/plugin-root`. Standalone installs can still use that compatibility
path.

The prepared Codex package retains the bundled manifests. For a guided install
of Codex or OpenCode by itself, the installer prepares the runtime and the
host's hook manifest or loader before saving confirmed settings through the
prepared interpreter. On Windows, the interpreter checks the staged settings
for local embedding prerequisites before it replaces the account file. If
registration fails after the guide confirms the save, it reports that setup
can be retried with the saved settings.

Codex commits its prepared runtime and hook cache before asking Codex to trust the
hooks. A later trust failure keeps that new runtime; follow the reported `/hooks`
recovery instructions. Registration, account configuration, and trust changes are
not an all-or-nothing transaction.

### Check an update

```bash
npx claude-smart status
npx claude-smart status --host codex
```

Shows the installed version and the version each recent session last used in a hook. Historical observations do not confirm that a session is still active. Codex uses `CODEX_HOME` when set; run installation and status with the same account home as your session. `status --host codex` also warns when a claude-smart hook is disabled in Codex's `config.toml` and says how to re-enable it; it never edits that file.

### Uninstall

```bash
npx claude-smart uninstall                    # Choose integrations interactively
npx claude-smart uninstall --host claude-code,codex  # Remove multiple integrations
npx claude-smart uninstall --host claude-code  # Claude Code
npx claude-smart uninstall --host codex       # Codex
npx claude-smart uninstall --host opencode    # OpenCode
```

Afterward, use `/reload-plugins` in Claude Code, start a new Codex session, or restart OpenCode. Learned data under `~/.reflexio/` and `~/.claude-smart/` is preserved and shared across hosts, so you can uninstall or switch hosts without losing skills or preferences.

Uninstall keeps saved configuration and the chosen background learning provider.
Removing an integration does not uninstall its agent CLI. Remaining integrations
can continue using the same learning provider. OpenCode removal applies to the
current project config, or its global config with `--global`.

If removing an integration breaks the shared command link, it is repaired from
the remaining runtimes, including registered Claude Code caches and local source
installs. A declared local source takes precedence over its older cache path;
discovery does not modify or delete that source.

For per-host details — what the installers and uninstallers touch, OpenCode config resolution and model/env overrides, and Windows notes — see [Host install notes](./DEVELOPER.md#host-install-notes) in DEVELOPER.md. Developing the plugin itself? See [Developing locally](./DEVELOPER.md#developing-locally).

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
- 🔌 **Local or managed storage** — in local mode, semantic search runs on an in-process ONNX embedder (all-MiniLM-L6-v2) and memories stay on this machine. Managed mode stores and processes memories on the configured cloud backend; device interaction buffers remain local.
- 🔎 **Hybrid search** — Skills and preferences are indexed with vector + BM25 search for fast, robust retrieval.
- 🧪 **Offline resilience** — If the reflexio backend is down, hooks buffer to disk; the next successful publish drains them.
- 🧰 **Manual correction marker** — `/claude-smart:learn` in Claude Code, or `bash ~/.reflexio/plugin-root/scripts/cli.sh learn` in Codex, flags the last turn as a correction so the extractor weights it heavily.

---

## Dashboard

A web UI for browsing session histories, inspecting preferences, and editing project-specific and shared skills. The local backend binds to `127.0.0.1:8071` by default. The dashboard auto-starts in both local and managed mode at **http://localhost:3001**. Its data-source selector defaults to your saved runtime mode; managed mode reads cloud totals without starting a local backend. Or run `/claude-smart:dashboard` in Claude Code to open it in your browser. In Codex, run `bash ~/.reflexio/plugin-root/scripts/dashboard-open.sh`.

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

Under the hood: hooks watch your turns, tool calls, and assistant replies, auto-flagging corrections (or anything you flag with `/claude-smart:learn`). At session end (or on `/claude-smart:learn`), [reflexio](https://github.com/ReflexioAI/reflexio) — the self-improving engine that powers claude-smart — extracts preferences and project-specific skills, then rolls durable patterns into shared skills. On each new user prompt, claude-smart searches for matching context and injects only the relevant hits, as a short index: one line per hit saying when it applies. The agent loads the full text of any hit that applies with `bash ~/.reflexio/plugin-root/scripts/cli.sh load --session <session-id> <id> [<id> ...]` (the injected header gives the exact command). Run `/claude-smart:show` or the equivalent CLI command to audit the current learned state. Everything runs on your machine.

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
| `~/.claude-smart/.env` | claude-smart config read by the hooks, backend, installer, and `npx claude-smart setup` — connection and learning policy (`REFLEXIO_URL` / `REFLEXIO_API_KEY`, `CLAUDE_SMART_LEARNING_PROVIDER` / `CLAUDE_SMART_LEARNING_MODEL`), plus execution overrides. Existing `CLAUDE_SMART_HOST` rows remain legacy fallback; installers no longer default or rewrite them. |
| `~/.claude-smart/claude-code/claude-smart/` | Claude Code's stable local copy of the active npm package, registered as the `reflexioai` marketplace. Claude Code runs the plugin in place from its `plugin/` dir. |
| `.claude/settings.local.json` or `~/.claude/settings.json` | Claude Code hook environment, such as `CLAUDE_SMART_CITATIONS`; use project-local settings for one repo or user settings for all projects. |
| `~/.codex/config.toml` | Codex plugin state, hook feature flags, and per-hook trust entries after `claude-smart install --host codex`. |
| `~/.codex/plugins/cache/reflexioai/claude-smart/<version>/` | Codex's cached install of the `claude-smart` plugin from the `ReflexioAI` marketplace. |
| `~/.codex/claude-smart/runtime/plugin/` | Codex's prepared Python environment and dashboard. Hooks in the cache execute this runtime; generated dependencies stay outside the validated plugin package. Both Codex paths use `CODEX_HOME` when set. |
| `opencode.json` / `opencode.jsonc`, or existing `.opencode/opencode.json*` | OpenCode local plugin config patched by `claude-smart install --host opencode`; the installer updates only the claude-smart entry in OpenCode's plugin list. |
| `~/.claude-smart/opencode/claude-smart/` | OpenCode's stable local copy of the active npm package. OpenCode config points here with `file://` so restarts do not re-resolve `claude-smart` from npm. |
| `~/.reflexio/plugin-root` | Self-healed symlink to the active plugin dir (managed by `ensure-plugin-root.sh` — written on install, refreshed each `SessionStart`). Claude Code slash commands and Codex shell-command helpers resolve through it, so don't delete it; if you do, the next session will recreate it. |
| `~/.claude-smart/sessions/{session_id}.jsonl` | Private per-session buffer: turns, tools, frozen requests and publication watermarks. Unacknowledged data remains here; deleting the buffer can lose pending interactions. |
| `~/.claude-smart/publish-outbox/` | Private Codex publication intents and shared worker locks. Contains destination fingerprints and completed-turn bounds; interaction content stays in session buffers. |
| `~/.claude-smart/node/current/` | Private Node.js/npm runtime used by hooks and the dashboard after install. |
| `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/` | Cached ONNX weights (~86 MB, downloaded once). Delete to force a re-download. |

Backends that require publication admission select their database in the same
atomic operation as a saved-configuration upgrade. If legacy work must drain,
refusal preserves the original configuration so the old writer can still open
it. Complete that work with the matching old backend before retrying; startup
does not discard history, switch to an empty database, or acknowledge charges.

Upgrading from `CLAUDE_SMART_USE_LOCAL_EMBEDDING` requires removing the retired
assignment after preserving the effective model, endpoint and timeout policy.
See the [embedding upgrade guide](https://github.com/ReflexioAI/claude-smart/blob/main/DEVELOPER.md#upgrade-from-the-old-embedding-flag), including the case where an old local default shadowed a written `off`.

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

Search hooks reserve a durable request identity in the session buffer before
retrieval. The corresponding response is published with that same identity;
retries keep it, and searches after a frozen publish belong to a later response.
Older buffered publishes keep their existing retry IDs. Cancelled turns do not
attach their retrieved-playbook references to a later prompt's answer. Search
alone does not supply a conversation or an outcome for offline tuning: the
response still needs to be published. Buffer writes and identity reservations
share a cross-process file lock on Linux, macOS, and native Windows.


### Short sessions and completion

Automatic session completion keeps Reflexio's usefulness judge enabled. Claude
Code's SessionEnd publishes an unsaved tail, then sends a completion hint even if
Stop already saved every message. Acceptance does not mean extraction has finished.
Explicit `/learn` remains a manual force operation.

With the updated Reflexio backend, publish-only integrations (including Codex,
OpenCode, and direct clients) evaluate short sessions after six hours without a new
publication in the same session. The operator can configure
`extraction_idle_timeout_seconds` (`21600` by default, `0` disables for new
publications). This fallback is a low-urgency policy, not proof a session ended.
Normal full-window and stride extraction still runs promptly. A negative judge
result produces no learning and is recorded as examined.

Optimizer policy is also owned by the Reflexio server. SessionStart no longer
turns optimization on or rewrites its budgets, adoption permissions or assistant
backend. Existing server settings are preserved; fresh servers keep their own
(disabled) optimizer default until explicitly configured. The old
`CLAUDE_SMART_ENABLE_OPTIMIZER` hook flag is no longer read. Removing or setting
that flag does not disable an optimizer already enabled on the server; change
`playbook_optimizer_config` on the server to do that.

The separately installed `claude-smart-optimizer-assistant` helper remains
available for explicitly configured local optimization. See the
[backend binding notes](./DEVELOPER.md#optimizer-backend-binding) before assigning
an executable path, especially when the server is remote.

Extraction cadence is owned by the Reflexio server: configure `window_size` and
`stride_size` there. Starting a client session preserves those settings. The
former hook overrides `CLAUDE_SMART_WINDOW_SIZE` and `CLAUDE_SMART_STRIDE_SIZE`
are no longer read; remove them from launchers and set the server configuration
explicitly when a benchmark needs a different cadence. Existing persisted values
are preserved; fresh servers use their own defaults.

Managed mode requires a server update; changing the plugin alone cannot update a
cloud server. An older server retains saved messages and logs that completion is
unsupported; the plugin does not republish or force extraction. Idle fallback
starts with new publications after backend upgrade; old saved sessions can be
explicitly finalized on the updated backend.

### Publish-time playbook evidence

User-playbook search receipts travel with the injected learning references in the
on-disk buffer and are sent unchanged on publish. Distinct receipts for one
playbook are retained, so a later revision cannot overwrite an earlier injection.
Reflexio stores exposure evidence with the interaction rather than writing it on
search. ID-only references remain supported but have unverified served versions.
Update the SDK/server together to receive this metadata; older search response
models may omit receipts. The server stores only a receipt digest in the
interaction, and client recovery uses it to compare the original submission.
Normal installs require `reflexio-ai>=0.2.32`; `plugin/uv.lock` resolves the
published SDK from PyPI. Keep the SDK/server upgrade coordinated: older SDK
response models can omit receipts, and older servers retain their existing
exposure behavior. ID-only publication remains compatible.
