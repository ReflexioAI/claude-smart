# Developer Guide

Internal notes for maintainers of `claude-smart`. End-user install instructions live in [README.md](./README.md); this file covers the release loop.

## Repository layout

| Path | Purpose |
| --- | --- |
| `plugin/` | Claude Code plugin — hooks, slash commands, install script, and the Python package |
| `plugin/src/claude_smart/` | Python package — hook handler, CLI, reflexio adapter |
| `plugin/bin/` | User-invoked helper scripts (e.g. `cs-cite`) installed into `~/.claude-smart/bin/` at runtime |
| `plugin/pyproject.toml` | Python manifest — shipped to PyPI via `uv build --project plugin` |
| `tests/` | Pytest suite for the Python package (run via `uv run --project plugin pytest tests/ -q` from repo root) |
| `bin/claude-smart.js` | Node wrapper so `npx claude-smart install` works |
| `package.json` | npm manifest — only ships `bin/`, `README.md`, `LICENSE` |
| `plugin/.claude-plugin/plugin.json` | Plugin metadata read by Claude Code |
| `.claude-plugin/marketplace.json` | Marketplace entry — `claude plugin marketplace add` reads this |
| `reflexio/` | Submodule — Apache 2.0, storage + search + extraction backend |
| `plugin/dashboard/` | Next.js management UI for interactions, preferences, skills, configuration |
| `Makefile` | Release automation |

## Environment variables

Tunables read by the plugin at runtime. Most users don't need to touch these — the installer writes the local-provider flags to `~/.reflexio/.env` and sensible defaults cover the rest. Hook-side variables must be set in the Claude Code process environment, such as a project `.claude/settings.local.json` `env` block or user-scoped `~/.claude/settings.json`, because hooks do not read Reflexio's backend `.env` file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_SMART_USE_LOCAL_CLI` | `0` (installer sets `1`) | Route generation through the active host CLI (`claude` for Claude Code, `codex` for Codex). Written to `~/.reflexio/.env` by `claude-smart install`. |
| `CLAUDE_SMART_USE_LOCAL_EMBEDDING` | `0` (installer sets `1`) | Use the in-process ONNX embedder (requires `chromadb`). Written to `~/.reflexio/.env` by `claude-smart install`. |
| `CLAUDE_SMART_ENABLE_OPTIMIZER` | enabled unless set to `0` | Hook-side env var that controls shared skill optimization and rollups during `SessionStart`. Set it in Claude Code settings, not `~/.reflexio/.env`. |
| `CLAUDE_SMART_CLI_PATH` | `claude` on Claude Code; Codex compatibility wrapper or `codex` on Codex | Override the executable Reflexio calls for local generation. |
| `CLAUDE_SMART_STATE_DIR` | `~/.claude-smart/sessions/` | Where the per-session JSONL buffer lives. |
| `CLAUDE_SMART_BACKEND_AUTOSTART` | `1` | Set to `0` to stop the SessionStart hook from spawning the reflexio backend on `localhost:8071`. |
| `CLAUDE_SMART_DASHBOARD_AUTOSTART` | `1` | Set to `0` to stop the SessionStart hook from spawning the Next.js dashboard on `localhost:3001`. |
| `CLAUDE_SMART_BACKEND_STOP_ON_END` | `0` | Set to `1` to tear down the backend at `SessionEnd` instead of leaving it long-lived. |
| `CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION` | `0` | Set to `1` (in env or `~/.reflexio/.env`) to have every `SessionStart` relink `~/.reflexio/plugin-root` to the session's active plugin dir. Off by default so a local-dev symlink (force-set by `setup-local-dev.sh`) is preserved when a remote-plugin session fires on the same machine. Turn on if you juggle remote and local-dev across different repos and want slash commands to follow whichever plugin this session loaded. |
| `REFLEXIO_URL` | `http://localhost:8071/` | Point the plugin at a non-local reflexio backend. |

## Embeddings

claude-smart uses an in-process ONNX embedder (Chroma's `all-MiniLM-L6-v2`, 384-dim, zero-padded to reflexio's 512-dim schema). The model weights are downloaded on first use (~80 MB, cached under `~/.cache/chroma/onnx_models/`) — after that, no network calls for embedding. Runtime cost is a few milliseconds per short document on CPU.

If you still want to use a cloud embedding provider (OpenAI, Gemini, etc.), omit `CLAUDE_SMART_USE_LOCAL_EMBEDDING` and set the corresponding API key in `~/.reflexio/.env` — reflexio will fall back to its standard provider-priority chain.

## Public terminology and storage names

User-facing surfaces should use these names:

- **Preference** — project-scoped facts or preferences ("prefers anyio over asyncio"). Free-form bullets.
- **Project-specific skill** — project-scoped rules with trigger and rationale ("when writing a script, use pathlib because os.path is error-prone").
- **Shared skill** — cross-project rollups of project-specific skills. Shared skills can be auto generated, persisted, or rejected.

Reflexio still exposes legacy storage/API names, and claude-smart keeps those identifiers in code because they are wire-format names:

| Public term | Reflexio storage/API term |
| --- | --- |
| Preference | `user_profiles`, `profile_id`, `UserProfile` |
| Project-specific skill | `user_playbooks`, `user_playbook_id`, `UserPlaybook` |
| Shared skill | `agent_playbooks`, `agent_playbook_id`, `AgentPlaybook` |

Do not use the storage/API terms in README copy, dashboard labels, slash-command descriptions, or injected context unless documenting raw Reflexio fields for maintainers.

## Dashboard

A standalone Next.js app at `plugin/dashboard/` that gives a visual view of what
claude-smart has learned:

- **Interactions** — session list + transcript reader backed by
  `~/.claude-smart/sessions/*.jsonl` (read server-side in
  `plugin/dashboard/app/api/sessions/route.ts`).
- **Preferences / Skills** — reflexio data fetched via a proxy route
  (`plugin/dashboard/app/api/reflexio/[...path]/route.ts`) that forwards to the URL
  configured in the top bar; defaults to `http://localhost:8071`.
- **Configure** — reads and writes backend/provider settings in `~/.reflexio/.env`
  and hook-side Claude Code settings in `.claude/settings.local.json`. Unknown
  `.env` keys (API secrets, user additions) are preserved on write and never
  returned to the browser.

Stack mirrors `reflexio/docs/` exactly (Next.js 16, Tailwind v4, shadcn
base-nova, Base UI primitives, Lucide icons, `next-themes`). Runs on port
3001 so it can coexist with reflexio's docs site on 3000.

```bash
cd plugin/dashboard
npm install
npm run dev     # http://localhost:3001
npm run build
npm run lint
```

**Next.js 16 caveat** — the same one reflexio/docs flags: APIs and
conventions differ from anything pre-16. Before touching route handlers or
dynamic-param types, consult `plugin/dashboard/node_modules/next/dist/docs/`.

## Versioning

`claude-smart` uses [semver](https://semver.org/). Four files carry the version and **must stay in lockstep** — if they drift, the npm wrapper, PyPI wheel, plugin metadata, and marketplace entry can claim different versions, and users see confusing mismatches in `claude plugin list` vs. `npm view claude-smart version`.

| File | Field |
| --- | --- |
| `package.json` | `.version` |
| `plugin/pyproject.toml` | `project.version` |
| `plugin/.claude-plugin/plugin.json` | `.version` |
| `.claude-plugin/marketplace.json` | `.plugins[0].version` |

Don't edit these by hand — use `make bump`.

## Release flow

### Prerequisites — one-time setup

1. **npm**: `npm login` (writes a token to `~/.npmrc`).
2. **PyPI**: create a project-scoped token at <https://pypi.org/manage/account/token/> and export it:
   ```bash
   export UV_PUBLISH_TOKEN=pypi-AgEN...
   ```
   Persist it in your shell rc if you release frequently.
3. **git**: push access to `origin` for the tag push at the end of `make release`.

### Every release

```bash
# 1. Make sure you're on a clean main (no staged/unstaged edits).
git checkout main
git pull --ff-only

# 2. Dry-run to confirm what will ship (optional but cheap).
make publish-dry
#   → npm: tarball includes bin/, .agents/plugins/marketplace.json, plugin/, LICENSE, README.md, package.json
#   → PyPI: dist/ contains a wheel + sdist

# 3. One-shot release. Bumps, commits, tags v$VERSION, publishes to both
#    registries, pushes the commit and tag.
make release VERSION=0.1.1
```

`make release` runs these steps in order and aborts on the first failure:

1. `check-version` — regex-validates `VERSION` as semver.
2. `check-clean` — refuses to run with a dirty working tree.
3. `bump` — rewrites the four version strings.
4. `git add` + `git commit -m "Release v$VERSION"`.
5. `git tag -a v$VERSION`.
6. `publish-npm` → `npm publish --access public`.
7. `publish-pypi` → `rm -rf dist/ && uv build && uv publish`.
8. `git push --follow-tags`.

Because publish happens **before** the push, a registry failure (e.g. auth expired) leaves the commit and tag local — you can fix the cause and re-run `make publish && git push --follow-tags` without re-bumping.

### Partial / advanced flows

```bash
# Just bump the version. Useful when you want to edit CHANGELOG, amend the
# commit message, or split the bump across branches.
make bump VERSION=0.1.1
git diff                                          # inspect
git commit -am "Release v0.1.1"
git tag -a v0.1.1 -m "Release v0.1.1"
make publish                                      # both registries
git push --follow-tags

# Only one registry (e.g. if the other published successfully and you're retrying).
make publish-npm
make publish-pypi

# Preview without uploading anything.
make publish-dry
```

`make help` prints the full target list.

### How users pick up the new release

Once published, end users update to the latest version with either:

```bash
npx claude-smart update     # or: uvx claude-smart update
```

Both wrap `claude plugin update claude-smart@reflexioai`. Users restart Claude Code to apply. Codex users rerun `npx claude-smart install --host codex`, then restart Codex after `/plugins` has upgraded the installed plugin.

## Pre-release checklist

Before running `make release`:

- [ ] `README.md` reflects any user-visible changes.
- [ ] Hook behavior, CLI flags, env vars are unchanged — or a migration note is in the README.
- [ ] `plugin/hooks/hooks.json` and `plugin/commands/*.md` render correctly in a local Claude Code session (smoke test: install from a local `directory` marketplace, start a session, run `/show`).
- [ ] `reflexio` submodule is at the pinned SHA you want to ship against. Bump it explicitly if needed:
  ```bash
  cd reflexio && git pull && cd .. && git add reflexio && git commit -m "Bump reflexio submodule"
  ```
- [ ] `npm publish --dry-run` tarball is still ~13 KB / 4 files — if it's larger, `files` in `package.json` is letting extra content through.
- [ ] If you touched `pyproject.toml` dependencies, `uv build` succeeds locally and the wheel's `METADATA` doesn't carry a `file://` path dep (common failure when `[tool.uv.sources]` leaks into the published wheel).

## Common failures and fixes

**`npm publish` fails with `403 Forbidden`.**
Usually means the version already exists. Check `npm view claude-smart versions`. npm does not allow re-uploading the same version — bump and re-release.

**`uv publish` fails with `400 File already exists`.**
Same as above for PyPI. Bump and re-release.

**`uv build` produces a wheel that fails to install with `No matching distribution found for reflexio-ai`.**
The `[tool.uv.sources]` override in `pyproject.toml` points `reflexio-ai` to a local path — fine for local dev, fatal for a published wheel. Either:
1. Publish `reflexio-ai` to PyPI first, then remove the `[tool.uv.sources]` block before `make release`, or
2. Drop `reflexio-ai` from the published package's `dependencies` and keep the install-time bootstrap (`plugin/scripts/smart-install.sh`) responsible for installing it inside the Claude Code plugin directory.

See README's "Step 1 — Install the plugin" section for the install flow that assumes option 2.

**`make release` committed and tagged but `npm publish` failed after.**
`publish` runs before `git push --follow-tags`, so nothing was pushed. Fix the cause (auth, network, registry), then:
```bash
make publish           # retry both, or publish-npm / publish-pypi individually
git push --follow-tags
```
If you need to *unwind* the local commit and tag:
```bash
git tag -d v$VERSION
git reset --hard HEAD~1
```

**`make bump` left `.bak` files behind.**
A sed failure mid-run can leave `*.bak` siblings. Safe to delete:
```bash
rm -f package.json.bak pyproject.toml.bak .claude-plugin/*.bak
```

## Developing locally

For iterating on claude-smart itself — editing hooks, the Python package, the dashboard, or the reflexio submodule — install from a clone and point the host at your working copy. End users don't need any of this; the `npx`/`uvx` install flow in the README covers them.

### Local install overview

You can flip each axis independently — e.g. local plugin + PyPI reflexio is a valid combination (you're touching hooks/slash commands/dashboard but not the backend), and so is remote plugin + local reflexio (smoke-test the shipped plugin against a patched backend).

| Axis | Local | Remote | Controlled by |
| --- | --- | --- | --- |
| **Plugin** — hooks, slash commands, Python package, dashboard | this repo's `plugin/` (marketplace `reflexioai-local`) | GitHub cache `~/.claude/plugins/cache/reflexioai/…` | `.claude/settings.local.json` → `enabledPlugins` |
| **reflexio** — backend, extraction, storage | vendored submodule at `reflexio/` (editable) | PyPI wheel `reflexio-ai` | `plugin/pyproject.toml` → `[tool.uv.sources]` |

Start every local install from a clone:

```bash
git clone --recurse-submodules https://github.com/ReflexioAI/claude-smart.git
cd claude-smart
```

### Claude Code local install

Use the one-shot setup when developing the Claude Code plugin, dashboard, or local `reflexio` submodule:

```bash
bash scripts/setup-local-dev.sh
```

Idempotent. This single script handles everything — see its header comment for the full list, but in summary it:

1. Initializes the `reflexio/` submodule.
2. Uncomments `[tool.uv.sources]` in `plugin/pyproject.toml` (absolute path to the submodule) and hides the divergence with `git update-index --skip-worktree`.
3. `uv sync` in `plugin/`.
4. Appends `CLAUDE_SMART_USE_LOCAL_CLI=1` and `CLAUDE_SMART_USE_LOCAL_EMBEDDING=1` to `~/.reflexio/.env`.
5. Prepares and registers the local marketplace (`local-marketplace/`, manifest name `reflexioai-local`) at user scope via `claude plugin marketplace add`.
6. Writes `.claude/settings.local.json` → enable `claude-smart@reflexioai-local`, disable `claude-smart@reflexioai`.
7. Force-sets `~/.reflexio/plugin-root` → `plugin/` so slash commands resolve to editable in-repo sources.

Then restart Claude Code. In the new session, `/plugin` should show `claude-smart@reflexioai-local` and should not also show the published `claude-smart@reflexioai`.

Uninstalling the local Claude Code plugin is a marketplace/settings cleanup:

```bash
claude plugin marketplace remove reflexioai-local
python3 - <<'PY'
import json
from pathlib import Path

path = Path(".claude/settings.local.json")
if path.exists():
    data = json.loads(path.read_text() or "{}")
    enabled = data.get("enabledPlugins")
    if isinstance(enabled, dict):
        enabled.pop("claude-smart@reflexioai-local", None)
        enabled.pop("claude-smart@reflexioai", None)
    path.write_text(json.dumps(data, indent=2) + "\n")
PY
rm -f ~/.reflexio/plugin-root
```

This leaves learned data under `~/.reflexio/` and `~/.claude-smart/` intact.

### Codex local install

Codex local development creates a durable local marketplace wrapper at `~/.claude/plugins/marketplaces/reflexioai`. The wrapper copies this checkout's `plugin/` directory to `./plugins/claude-smart`, which keeps Codex's plugin folder name aligned with the manifest name while preserving the existing Claude Code layout:

```bash
uv run --project plugin claude-smart install --host codex
```

Then fully restart Codex so hooks reload. The command installs `claude-smart` into `~/.codex/plugins/cache/reflexioai/claude-smart/<version>/`, enables `[plugins."claude-smart@reflexioai"]`, enables Codex's hook feature flags, and seeds the per-hook trust entries in `~/.codex/config.toml`; `/plugins` should show it as installed from the **ReflexioAI** marketplace. Running `codex features enable plugin_hooks` by itself is not enough because it does not install the plugin or trust the individual hook entries.

If you install or toggle `claude-smart` manually from `/plugins`, rerun `npx claude-smart install --host codex` (or the `uv run` equivalent) once afterward so the hook feature flags, plugin cache, and claude-smart hook trust state are re-prepared.

For live hook iteration, point Codex at this checkout before the cache:

```bash
mkdir -p ~/plugins
ln -sfn "$PWD/plugin" ~/plugins/claude-smart
```

This symlink is only for local plugin development; normal `npx claude-smart install --host codex` users should not create it. The Codex hook resolver checks `~/plugins/claude-smart` before the Codex cache, so Python, hook, and script edits in `$PWD/plugin` are picked up without reinstalling the marketplace. Without that dev symlink, rerun `uv run --project plugin claude-smart install --host codex` after plugin edits so the wrapper copy is refreshed.

Uninstall local Codex support with the CLI. This removes the Codex marketplace registration, installed plugin config, hook trust state, and Codex plugin cache while preserving learned data:

```bash
uv run --project plugin claude-smart uninstall --host codex
rm -f ~/plugins/claude-smart
```

If `/plugins` still shows a stale pre-rename install, remove the legacy Codex state too:

```bash
python3 - <<'PY'
from pathlib import Path

path = Path.home() / ".codex" / "config.toml"
text = path.read_text()
lines = text.splitlines(keepends=True)
drop_prefixes = (
    '[marketplaces.claude-smart-local]',
    '[plugins."claude-smart@claude-smart-local"]',
    '[hooks.state."claude-smart@claude-smart-local:',
)
out = []
dropping = False
for line in lines:
    if line.startswith("[") and line.rstrip().endswith("]"):
        dropping = any(line.startswith(prefix) for prefix in drop_prefixes)
    if not dropping:
        out.append(line)
path.write_text("".join(out))
PY
rm -rf ~/.codex/plugins/cache/claude-smart-local
```

Restart Codex after uninstalling. Learned data under `~/.reflexio/` and `~/.claude-smart/` is intentionally preserved.

### Switching Claude Code plugin source (local ↔ remote)

Edit `.claude/settings.local.json` to flip which variant is enabled (both must be present so the other one is explicitly disabled and can't shadow):

```jsonc
// local plugin
"enabledPlugins": {
  "claude-smart@reflexioai-local": true,
  "claude-smart@reflexioai": false
}
```
```jsonc
// remote plugin
"enabledPlugins": {
  "claude-smart@reflexioai-local": false,
  "claude-smart@reflexioai": true
}
```

Prerequisites (one-time, already done by `setup-local-dev.sh`):
- Local marketplace prepared and registered at user scope: `claude plugin marketplace add $PWD/local-marketplace`.
- Remote marketplace registered at user scope (`~/.claude/settings.json` → `extraKnownMarketplaces.reflexioai` → `github: ReflexioAI/claude-smart`).

**Apply the change:** restart Claude Code. In the new session, `/plugin` must show **exactly one** `claude-smart@…` entry (if it shows two, the scopes are stacking).

Optional: if you juggle local and remote across different repos on the same machine and want slash commands to always follow whichever plugin this session loaded, set `CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION=1` in `~/.reflexio/.env`. Off by default so a local-dev symlink is preserved when a remote-plugin session fires on the same machine.

#### What's picked up automatically vs. what needs a restart (local plugin mode)

- `plugin/src/claude_smart/` — Python package edits are live on the next hook invocation (hooks shell out via `uv run`). No restart needed.
- `plugin/commands/*.md`, `plugin/hooks/hooks.json` — picked up on the next Claude Code session.
- `plugin/dashboard/` — the dashboard runs `npm run start` against prebuilt `.next/`, long-lived across sessions. Rebuild + restart: `/claude-smart:restart` or `claude-smart restart`.

### Switching axis 2 — reflexio source (submodule ↔ PyPI)

Edit `plugin/pyproject.toml`. The `[tool.uv.sources]` block is what decides.

```toml
# local submodule (editable)
[tool.uv.sources]
reflexio-ai = { path = "/absolute/path/to/repo/reflexio", editable = true }
```
```toml
# PyPI — just delete or comment out the block above
# [tool.uv.sources]
# reflexio-ai = { path = "/absolute/path/to/repo/reflexio", editable = true }
```

**Important — absolute path required.** `uv run --project <symlink>` resolves relative paths against the literal symlink, not its realpath. Since slash commands pass `~/.reflexio/plugin-root` as `--project`, a relative `../reflexio` would resolve to `$HOME/.reflexio/reflexio` (nonexistent). Always use the absolute path.

**Note — git invisibility.** `setup-local-dev.sh` runs `git update-index --skip-worktree plugin/pyproject.toml plugin/uv.lock`, so edits to these files are **invisible** to `git status`. If you need to commit a genuine change:

```bash
git update-index --no-skip-worktree plugin/pyproject.toml plugin/uv.lock
# stage hunks selectively, commit
git update-index --skip-worktree plugin/pyproject.toml plugin/uv.lock
```

**Apply the change:**

```bash
cd plugin && uv sync --quiet
claude-smart restart                  # or /claude-smart:restart inside Claude Code
```

`uv sync` re-resolves the dependency. The **backend service must be restarted** — it's a long-lived process on port 8071 with the old `reflexio-ai` already imported in memory, so editing `pyproject.toml` alone does nothing until the service reloads.

Verify:

```bash
uv run --project plugin python -c "import reflexio, os; print(reflexio.__version__); print(os.path.dirname(reflexio.__file__))"
```

- Path into `reflexio/reflexio/` under this repo → **submodule** (editable).
- Path into `…/site-packages/reflexio/` → **PyPI**.

### Manual setup (alternative to setup-local-dev.sh)

If you want to understand the pieces or do a subset:

```bash
# 1. Clone + submodule
git clone --recurse-submodules https://github.com/ReflexioAI/claude-smart.git
cd claude-smart
git submodule update --init --recursive    # if you forgot --recurse-submodules

# 2. Python deps
cd plugin && uv sync && cd ..
# Creates plugin/.venv/ and registers claude-smart + claude-smart-hook scripts.

# 3. Local providers (no API key needed)
mkdir -p ~/.reflexio
grep -q '^CLAUDE_SMART_USE_LOCAL_CLI=' ~/.reflexio/.env 2>/dev/null \
  || echo 'CLAUDE_SMART_USE_LOCAL_CLI=1' >> ~/.reflexio/.env
grep -q '^CLAUDE_SMART_USE_LOCAL_EMBEDDING=' ~/.reflexio/.env 2>/dev/null \
  || echo 'CLAUDE_SMART_USE_LOCAL_EMBEDDING=1' >> ~/.reflexio/.env
# On first use the embedder downloads ~80 MB ONNX weights to ~/.cache/chroma/onnx_models/.

# 4. (Optional) start the backend manually instead of letting SessionStart spawn it
cd plugin && BACKEND_PORT=8071 uv run reflexio services start --only backend --no-reload
# Health check: curl http://localhost:8071/health  →  {"status":"healthy"}
# Stop:         uv run reflexio services stop
```

Expected backend log lines:

```
Registered claude-code LiteLLM provider (cli=/path/to/claude)
Local embedding provider enabled (model=local/minilm-l6-v2)
Auto-detected LLM providers (priority order): ['claude-code', 'local']
Primary provider for generation: claude-code
Embedding provider: local
Application startup complete.
```

Then flip the two axes by hand per the sections above.

### Sanity check

Inside Claude Code:

```
/show
```

On a fresh project: `_No skills or preferences yet for project <name>._`. Have a conversation, correct Claude on something (e.g. `"no, don't use X — use Y"`), then run `/learn` — this flags the previous turn as a correction and forces extraction immediately. After ~20–30 seconds, `/show` surfaces the new skill or preference.

### Verifying which mode is live

```bash
# Axis 1 — plugin source
jq '.enabledPlugins' ~/.claude/settings.json
jq '.enabledPlugins' .claude/settings.local.json 2>/dev/null
readlink ~/.reflexio/plugin-root
# → $PWD/plugin  (local)   or   ~/.claude/plugins/cache/reflexioai/claude-smart/<ver>  (remote)

# Axis 2 — reflexio source
uv run --project plugin python -c "import reflexio, os; print(reflexio.__version__); print(os.path.dirname(reflexio.__file__))"
```

### Exercising the install CLIs against a local marketplace

Useful when you're modifying the `install` subcommand itself and want to test without re-publishing:

```bash
uv run --project plugin claude-smart install --source $PWD    # Python path
node bin/claude-smart.js install --source $PWD                # Node path
uv run --project plugin claude-smart install --host codex      # Codex repo-local path
node bin/claude-smart.js install --host codex                  # Codex packaged-marketplace path
```

The Claude Code install commands accept either a GitHub `owner/repo` ref or an absolute path to a local directory containing `.claude-plugin/marketplace.json`. The Codex Python path creates a local marketplace wrapper that copies this checkout's `plugin/`; the Codex Node path exercises the packaged no-clone flow by copying the bundled plugin into `~/.claude/plugins/marketplaces/reflexioai/plugins/claude-smart` before registering it with Codex. Both Codex paths ask `codex app-server` for the current claude-smart hook hashes and write matching trust state to `~/.codex/config.toml`.

## Tests

```bash
uv run --project plugin pytest tests/ -q   # claude-smart package tests (from repo root)
cd reflexio && uv run pytest tests/server/llm/ -q -o 'addopts='   # reflexio patch tests
```

Run both locally before `make release`. There's no CI gate today — the release flow trusts the maintainer.

### Exercising a hook handler directly

Useful for debugging without a live Claude Code session:

```bash
echo '{"session_id":"dev-1","source":"startup","cwd":"'"$PWD"'"}' \
  | uv run --project plugin python -m claude_smart.hook session-start
```

## Manual smoke tests — credit-stall notification

These exercise the credit-stall detection path end-to-end against real reflexio state.

### 1. Force a stall via SQLite

```bash
sqlite3 ~/.reflexio/data/reflexio.db <<'SQL'
UPDATE stall_state SET stalled=1, reason='billing_error',
  stalled_at=datetime('now'), notified_in_cc=0,
  error_message='manual smoke test';
SQL
```

Open `http://localhost:3001` — the banner should appear. Start a new Claude Code session — the SessionStart banner should appear once. Start a second session immediately — no banner (notified_in_cc was flipped).

Clear:

```bash
sqlite3 ~/.reflexio/data/reflexio.db "UPDATE stall_state SET stalled=0, reason=NULL, notified_in_cc=0 WHERE id=1;"
```

### 2. Real auth stall

`claude /logout`, then trigger any reflexio extraction (e.g. `/learn`). Expect `reason='auth_error'`, then a `/login` banner on the next SessionStart.

### 3. Real billing stall

Reproducible only against an exhausted Agent SDK credit pool. Capture the stream-json output as a fixture under `reflexio/tests/server/llm/fixtures/` so the integration test stays current as Anthropic's error format evolves.
