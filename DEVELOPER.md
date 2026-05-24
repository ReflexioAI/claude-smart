# Developer Guide

Internal notes for maintainers of `claude-smart`. End-user install instructions live in [README.md](./README.md); this file covers the release loop.

## Repository layout

| Path | Purpose |
| --- | --- |
| `plugin/` | Claude Code plugin — hooks, slash commands, install script, and the Python package |
| `plugin/src/claude_smart/` | Python package — hook handler, CLI, reflexio adapter |
| `plugin/pyproject.toml` | Python manifest — shipped to PyPI via `uv build --project plugin` |
| `tests/` | Pytest suite for the Python package (run via `uv run --project plugin pytest tests/ -q` from repo root) |
| `bin/claude-smart.js` | Node wrapper so `npx claude-smart install` works |
| `package.json` | npm manifest — ships `bin/`, marketplace metadata, `plugin/`, `README.md`, and `LICENSE` |
| `plugin/.claude-plugin/plugin.json` | Plugin metadata read by Claude Code |
| `.claude-plugin/marketplace.json` | Marketplace entry — `claude plugin marketplace add` reads this |
| `reflexio.lock.json` | Reflexio provenance — records the PyPI baseline or generated vendor bundle commit |
| `plugin/vendor/reflexio/` | Generated release-only Reflexio bundle for npm artifacts; gitignored, not committed |
| `plugin/dashboard/` | Next.js management UI for interactions, preferences, skills, configuration |
| `Makefile` | Release automation |

## Environment variables

Tunables read by the plugin at runtime. Most users don't need to touch these — the installer writes the local-provider flags to `~/.reflexio/.env` and sensible defaults cover the rest. Hook-side variables must be set in the Claude Code process environment, such as a project `.claude/settings.local.json` `env` block or user-scoped `~/.claude/settings.json`, because hooks do not read Reflexio's backend `.env` file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_SMART_USE_LOCAL_CLI` | `0` (installer sets `1`) | Route generation through the active host CLI (`claude` for Claude Code, `codex` for Codex). Written to `~/.reflexio/.env` by `claude-smart install`. |
| `CLAUDE_SMART_USE_LOCAL_EMBEDDING` | `0` (installer sets `1`) | Route embeddings to the shared local embedding daemon on `localhost:8072`. Written to `~/.reflexio/.env` by `claude-smart install`. |
| `REFLEXIO_EMBEDDING_PROVIDER` | `local_service` when local embedding is enabled | Embedding provider mode: `cloud`, `local_service`, `internal_service`, `inprocess`, or `off`. |
| `REFLEXIO_EMBEDDING_SERVICE_URL` | `http://127.0.0.1:$EMBEDDING_PORT` | OpenAI-compatible embedding endpoint used by `local_service` and `internal_service`. |
| `EMBEDDING_PORT` | `8072` | Local embedding daemon port. Shared by Claude Code and Codex on the same machine. |
| `CLAUDE_SMART_ENABLE_OPTIMIZER` | enabled unless set to `0` | Hook-side env var that controls shared skill optimization and rollups during `SessionStart`. Set it in Claude Code settings, not `~/.reflexio/.env`. |
| `CLAUDE_SMART_CITATIONS` | `on` | Controls the final `✨ claude-smart rule applied` marker instruction. `on` (default) injects a compact instruction asking the assistant to cite only memories that materially changed the answer. `off` skips injection of the citation instruction entirely and strips any stray marker line from the assistant text before publishing. Legacy values `auto` and `marker-only` are accepted as enabled aliases. Set in Claude Code settings (hook env), not `~/.reflexio/.env`. |
| `CLAUDE_SMART_CITATION_LINK_STYLE` | `markdown` | Controls the visible links in the final `✨ claude-smart rule applied` marker. `markdown` asks the model to emit `[human title](dashboard URL)` links. `osc8` asks it to emit terminal-native OSC 8 hyperlinks so the human title can be clickable without showing the URL; unsupported terminals should fall back to markdown links. |
| `CLAUDE_SMART_DASHBOARD_URL` | `http://localhost:3001` | Base URL used in injected citation targets and the final `✨ claude-smart rule applied` marker links. Override only if the local dashboard is exposed on another host or port. Model-facing citation links use short `/rules/{citationId}` resolver URLs; canonical direct links remain `/skills/project/{id}`, `/skills/shared/{id}`, and `/preferences/project/{id}`. `/preferences/{id}` remains a compatibility alias. |
| `CLAUDE_SMART_CLI_PATH` | `claude` on Claude Code; Codex compatibility wrapper or `codex` on Codex | Override the executable Reflexio calls for local generation. |
| `CLAUDE_SMART_STATE_DIR` | `~/.claude-smart/sessions/` | Where the per-session JSONL buffer lives. |
| `CLAUDE_SMART_NODE_LTS_MAJOR` | `22` | Major version used when the installer downloads private Node.js/npm into `~/.claude-smart/node/current`. |
| `CLAUDE_SMART_NODE_BASE_URL` | `https://nodejs.org/dist/latest-v${CLAUDE_SMART_NODE_LTS_MAJOR}.x` | Override the Node.js download base URL for tests, mirrors, or recovery. |
| `CLAUDE_SMART_BACKEND_AUTOSTART` | `1` | Set to `0` to stop the SessionStart hook from spawning the reflexio backend on `localhost:8071`. |
| `CLAUDE_SMART_DASHBOARD_AUTOSTART` | `1` | Set to `0` to stop the SessionStart hook from spawning the Next.js dashboard on `localhost:3001`. |
| `CLAUDE_SMART_BACKEND_STOP_ON_END` | `0` | Set to `1` to tear down the backend at `SessionEnd` instead of leaving it long-lived. |
| `CLAUDE_SMART_PLUGIN_ROOT_FOLLOW_SESSION` | `0` | Set to `1` (in env or `~/.reflexio/.env`) to have every `SessionStart` relink `~/.reflexio/plugin-root` to the session's active plugin dir. Off by default so a local-dev symlink (force-set by `setup-local-dev.sh`) is preserved when a remote-plugin session fires on the same machine. Turn on if you juggle remote and local-dev across different repos and want slash commands to follow whichever plugin this session loaded. |
| `REFLEXIO_URL` | `http://localhost:8071/` | Point the plugin at a non-local reflexio backend. |

## Embeddings

claude-smart uses one shared local embedding daemon by default. The backend starts `reflexio embeddings serve` on `127.0.0.1:8072` when `CLAUDE_SMART_USE_LOCAL_EMBEDDING=1`, then every backend worker and host process calls its OpenAI-compatible `/v1/embeddings` endpoint instead of loading a model in each process.

Supported local models are `local/nomic-embed-v1.5`, `local/nomic-embed-text-v1.5`, and `local/minilm-l6-v2`. The daemon owns exactly one model for its lifetime; start a second daemon on another port if you need to serve a different model concurrently.

If you still want to use a cloud embedding provider (OpenAI, Gemini, etc.), omit `CLAUDE_SMART_USE_LOCAL_EMBEDDING` and set the corresponding API key in `~/.reflexio/.env` — reflexio will fall back to its standard provider-priority chain. For hosted Reflexio, prefer a managed embedding provider or a scalable `REFLEXIO_EMBEDDING_PROVIDER=internal_service` endpoint instead of a single-machine daemon.

## Install dependency policy

Vanilla install support means the user has installed the host (`claude` or
`codex`) and the chosen installer runner (`npx` needs Node, `uvx` needs uv), but
does not need global Python, uv, Node/npm for plugin runtime, npm packages, or
embedding model files. The plugin bootstraps runtime dependencies into user
state:

| Dependency | Installed/managed by | Location |
| --- | --- | --- |
| Python 3.12 env and Python packages | `uv sync --locked --python 3.12` | plugin `.venv` |
| Runtime uv | installer if missing | `~/.local/bin` or `~/.cargo/bin` |
| Runtime Node.js/npm | installer if missing | `~/.claude-smart/node/current` |
| Dashboard packages/build | installer or first dashboard start | `plugin/dashboard/node_modules`, `plugin/dashboard/.next` |
| Local embedding model | Chroma/ONNX on first use | `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/` |

Supported native vanilla targets are Apple Silicon macOS 14+ and Windows x64;
Linux remains supported when host hooks run locally and wheels are available.
Intel Mac, macOS 13 or older, and Windows ARM should fail before dependency sync
with a visible unsupported-platform message because the current ML dependency
stack does not publish a complete native wheel set for those targets.

On native Windows, Claude Code hooks still need a Git Bash-compatible `bash`
until Claude Code exposes a single cross-platform hook command shape.

When changing dependencies, verify:

```bash
uv sync --project plugin --locked --dry-run --python 3.12 --python-platform x86_64-pc-windows-msvc
uv sync --project plugin --locked --dry-run --python 3.12
cd plugin/dashboard && npm ci && npm run build
```

Also check the published package surface:

```bash
npm pack --dry-run --json
```

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

For a vendor-mode Reflexio release, run `make release-npm VERSION=...` after
`bash scripts/release-with-reflexio.sh`. The generated vendor bundle is only in
the npm tarball; `make release` intentionally refuses to publish PyPI while
`reflexio.lock.json` says `source=vendor`.

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

# Vendor-mode Reflexio release (npm only).
make release-npm VERSION=0.1.1

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
- [ ] If claude-smart needs new Reflexio behavior, choose a Reflexio release mode:
  - PyPI mode: `reflexio-ai` is published, then `REFLEXIO_RELEASE_SOURCE=pypi bash scripts/release-with-reflexio.sh` updates `plugin/pyproject.toml`, `plugin/uv.lock`, and `reflexio.lock.json`.
  - Vendor mode: `bash scripts/release-with-reflexio.sh` generates `plugin/vendor/reflexio` and updates `reflexio.lock.json`; keep the generated vendor directory in place until npm publish completes.
- [ ] `python scripts/check-reflexio-lock.py` passes.
- [ ] `npm pack --dry-run --json` includes the root wrapper, marketplace metadata, plugin payload, dashboard sources, README, and LICENSE, and excludes `.venv`, `node_modules`, `.next/cache`, and Python caches.
- [ ] If you touched `pyproject.toml` dependencies, `uv build` succeeds locally and the wheel's `METADATA` does not carry any local path dependency.

## Common failures and fixes

**`npm publish` fails with `403 Forbidden`.**
Usually means the version already exists. Check `npm view claude-smart versions`. npm does not allow re-uploading the same version — bump and re-release.

**`uv publish` fails with `400 File already exists`.**
Same as above for PyPI. Bump and re-release.

**`uv build` produces a wheel that fails to install with `No matching distribution found for reflexio-ai`.**
The committed `plugin/pyproject.toml` must resolve `reflexio-ai` from PyPI. If this release only needs Reflexio through the npm plugin artifact, use vendor mode instead of raising the PyPI dependency to an unpublished Reflexio version:

```bash
bash scripts/release-with-reflexio.sh
```

If this release needs the PyPI package dependency itself to require a newer published Reflexio version, publish that Reflexio version first, then run:

```bash
REFLEXIO_RELEASE_SOURCE=pypi bash scripts/release-with-reflexio.sh
```

For non-standard worktree layouts, point the helper at the intended Reflexio checkout:

```bash
REFLEXIO_PATH=/path/to/reflexio-main bash scripts/release-with-reflexio.sh
```

Vendor mode verifies that the Reflexio checkout is clean, exports the committed HEAD into `plugin/vendor/reflexio`, and records the commit in `reflexio.lock.json`. PyPI mode additionally verifies that the Reflexio checkout matches `origin/main`, is tagged `v<version>`, and that `reflexio-ai==version` exists on PyPI.

Manual equivalent:

```bash
python scripts/sync-reflexio-dep.py --write --check-pypi --release-checks
uv lock --project plugin --upgrade-package reflexio-ai
python scripts/check-reflexio-lock.py
```

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

For iterating on claude-smart itself — editing hooks, the Python package, the dashboard, or a side-by-side Reflexio checkout — install from a clone and point the host at your working copy. End users don't need any of this; the `npx`/`uvx` install flow in the README covers them.

### Local install overview

You can flip each axis independently — e.g. local plugin + PyPI Reflexio is a valid combination when you're touching hooks/slash commands/dashboard but not the backend. Local Reflexio development uses an editable install in the plugin virtualenv; the committed package metadata still resolves `reflexio-ai` from PyPI.

| Axis | Local | Remote | Controlled by |
| --- | --- | --- | --- |
| **Plugin** — hooks, slash commands, Python package, dashboard | this repo's `plugin/` (marketplace `reflexioai-local`) | GitHub cache `~/.claude/plugins/cache/reflexioai/…` | `.claude/settings.local.json` → `enabledPlugins` |
| **Reflexio** — backend, extraction, storage | side-by-side checkout, usually `../reflexio` (editable install) | PyPI wheel `reflexio-ai`, or generated npm-only `plugin/vendor/reflexio` for fast releases | `scripts/use-local-reflexio.sh` for local dev; `reflexio.lock.json` + `plugin/pyproject.toml` for PyPI releases; `reflexio.lock.json` + generated vendor bundle for npm fast releases |

Clone the repos side-by-side for local cross-repo development:

```bash
mkdir -p workspace
cd workspace
git clone https://github.com/ReflexioAI/reflexio.git
git clone https://github.com/ReflexioAI/claude-smart.git
cd claude-smart
bash scripts/use-local-reflexio.sh
```

Edit both repos as needed. Open one PR in `reflexio` and one PR in `claude-smart`, then link the Reflexio PR from the claude-smart PR.

### Local install

Use the one-shot setup when developing the plugin, dashboard, or local Reflexio checkout:

```bash
bash scripts/setup-local-dev.sh                         # Claude Code local setup
bash scripts/setup-local-dev.sh --host codex            # Codex local setup
bash scripts/setup-local-dev.sh --host both             # Claude Code + Codex
bash scripts/setup-local-dev.sh --read-only             # Local setup without publishing interactions
```

Idempotent. This single script handles the shared local-dev prep for every host — see its header comment for the full list, but in summary it:

1. Runs `scripts/use-local-reflexio.sh`, which syncs `plugin/` and installs the side-by-side Reflexio checkout as editable without changing committed dependency metadata.
2. Appends `CLAUDE_SMART_USE_LOCAL_CLI=1` and `CLAUDE_SMART_USE_LOCAL_EMBEDDING=1` to `~/.reflexio/.env`.
3. For `--host claude-code` (the default), prepares and refreshes the local marketplace (`local-marketplace/`, manifest name `reflexioai-local`) at user scope by replacing any stale `reflexioai-local` registration before running `claude plugin marketplace add`.
4. For `--host claude-code`, writes `.claude/settings.local.json` → enable `claude-smart@reflexioai-local`, disable `claude-smart@reflexioai`.
5. For `--host claude-code`, force-sets `~/.reflexio/plugin-root` → `plugin/` so slash commands resolve to editable in-repo sources.
6. For `--host codex`, runs `node bin/claude-smart.js install --host codex` so the installed Codex hooks are patched through the JSON-safe `scripts/codex-hook.js` adapter.
7. For `--host codex`, writes a local-only `reflexio-ai` source override into the generated Codex marketplace/cache copies and installs the same side-by-side Reflexio checkout as editable into Codex's copied plugin cache venv, because Codex hooks run from `~/.codex/plugins/cache/...` instead of directly from this checkout.
8. For `--host both`, force-sets `~/.reflexio/plugin-root` back to editable `plugin/` after the Codex install step, since the Codex installer may point it at the copied cache.
9. Refreshes the local dashboard process. The dashboard health endpoint exposes the serving plugin root, so setup can stop a stale claude-smart dashboard from an older cache while leaving a foreign app on port `3001` alone.

Then restart the target host. In a new Claude Code session, `/plugin` should show `claude-smart@reflexioai-local` and should not also show the published `claude-smart@reflexioai`. In Codex, `/plugins` should show `claude-smart` installed from the **ReflexioAI** marketplace.

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

Codex local development creates a durable local marketplace wrapper at `~/.claude/plugins/marketplaces/reflexioai`. The one-shot setup runs the Node installer path so the copied plugin has Codex-safe hook commands:

```bash
bash scripts/setup-local-dev.sh --host codex
```

To exercise just the Python installer path directly, run:

```bash
uv run --project plugin claude-smart install --host codex
```

Then fully restart Codex so hooks reload. The command installs `claude-smart` into `~/.codex/plugins/cache/reflexioai/claude-smart/<version>/`, enables `[plugins."claude-smart@reflexioai"]`, enables Codex's hook feature flags, seeds the per-hook trust entries in `~/.codex/config.toml`, and refreshes any currently running claude-smart dashboard so port `3001` does not keep serving an older cache; `/plugins` should show it as installed from the **ReflexioAI** marketplace. Running `codex features enable plugin_hooks` by itself is not enough because it does not install the plugin or trust the individual hook entries. The Python installer is useful for installer development, but the Node installer is the supported local setup path for live Codex sessions because it patches hook commands through `codex-hook.js`.

The packaged Node installer path (`node bin/claude-smart.js install --host codex`, or published `npx claude-smart install --host codex`) additionally bootstraps private Node/npm plus the uv-managed Python runtime, builds the dashboard, and patches Codex hooks to the Node wrapper. The Python `uv run` path is intentionally for local development from an already-prepared checkout.

If you install or toggle `claude-smart` manually from `/plugins`, rerun `npx claude-smart install --host codex` (or the `uv run` equivalent) once afterward so the hook feature flags, plugin cache, and claude-smart hook trust state are re-prepared.

For live hook iteration, rerun setup after plugin edits so the patched marketplace/cache copy is refreshed:

```bash
bash scripts/setup-local-dev.sh --host codex
```

Avoid `~/plugins/claude-smart` for this plugin unless you also provide a patched hook manifest; Codex resolves that path before the cache, and the raw source `hooks/codex-hooks.json` uses Claude-style hook commands that can emit multiple JSON objects. The Node installer patches the copied hook manifest to call `scripts/codex-hook.js`, which normalizes hook stdout for Codex.

Uninstall local Codex support with the CLI. This stops local claude-smart services, removes the Codex marketplace registration, installed plugin config, hook trust state, and Codex plugin cache while preserving learned data:

```bash
uv run --project plugin claude-smart uninstall --host codex
rm -f ~/plugins/claude-smart  # legacy local-dev symlink, if present
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

### Switching axis 2 — Reflexio source (editable checkout ↔ PyPI/vendor)

Use the helper script for local Reflexio development. It installs the side-by-side checkout into `plugin/.venv` as editable without writing local paths into `plugin/pyproject.toml`:

```bash
bash scripts/use-local-reflexio.sh
# or
REFLEXIO_PATH=/absolute/path/to/reflexio bash scripts/use-local-reflexio.sh
```

If you cloned both repos and are unsure which Reflexio source claude-smart is
actually importing, run:

```bash
make doctor-reflexio
```

If a sibling `../reflexio` checkout exists but the plugin environment is still
using PyPI, the doctor exits non-zero and prints the fix command.

Return to the locked PyPI dependency by syncing the plugin environment:

```bash
uv sync --project plugin --locked --reinstall-package reflexio-ai
claude-smart restart                  # or /claude-smart:restart inside Claude Code
```

The **backend service must be restarted** after switching sources — it's a long-lived process on port 8071 with the old `reflexio-ai` already imported in memory.

Verify:

```bash
uv run --project plugin --no-sync python -c "import reflexio, os; print(reflexio.__version__); print(os.path.dirname(reflexio.__file__))"
```

- Path into `workspace/reflexio/reflexio/` or another checkout → **editable local Reflexio**.
- Path into `…/site-packages/reflexio/` → **PyPI** or an installed generated vendor copy.

For installed npm releases, the setup wrapper runs `uv sync --locked` first. If the npm tarball contains `plugin/vendor/reflexio`, it then installs that bundled Reflexio source into the plugin environment. User installs do not clone GitHub.

### Release flow with Reflexio

If claude-smart does not need new Reflexio behavior, release claude-smart only; do not change the `reflexio-ai` dependency.

If claude-smart needs unpublished Reflexio behavior, use vendor mode. The committed package metadata still points at the PyPI baseline, but the npm artifact carries the exact Reflexio source snapshot:

```bash
bash scripts/release-with-reflexio.sh
```

For non-standard worktree layouts:

```bash
REFLEXIO_PATH=/path/to/reflexio-main bash scripts/release-with-reflexio.sh
```

Commit:

```text
reflexio.lock.json
```

Keep generated `plugin/vendor/reflexio` in place until npm publish completes; it is gitignored but included in the npm tarball. Then release claude-smart to npm:

```bash
make release-npm VERSION=<new-claude-smart-version>
```

If claude-smart needs a newer published Reflexio package dependency, merge Reflexio, tag the release commit as `v<version>`, publish `reflexio-ai`, then run:

```bash
REFLEXIO_RELEASE_SOURCE=pypi bash scripts/release-with-reflexio.sh
```

Commit:

```text
plugin/pyproject.toml
plugin/uv.lock
reflexio.lock.json
```

Then release claude-smart.

### Manual setup (alternative to setup-local-dev.sh)

If you want to understand the pieces or do a subset:

```bash
# 1. Clone repos side-by-side
mkdir -p workspace
cd workspace
git clone https://github.com/ReflexioAI/reflexio.git
git clone https://github.com/ReflexioAI/claude-smart.git
cd claude-smart

# 2. Python deps
bash scripts/use-local-reflexio.sh
# Creates plugin/.venv/, syncs claude-smart, and installs ../reflexio editable.

# 3. Local providers (no API key needed)
mkdir -p ~/.reflexio
grep -q '^CLAUDE_SMART_USE_LOCAL_CLI=' ~/.reflexio/.env 2>/dev/null \
  || echo 'CLAUDE_SMART_USE_LOCAL_CLI=1' >> ~/.reflexio/.env
grep -q '^CLAUDE_SMART_USE_LOCAL_EMBEDDING=' ~/.reflexio/.env 2>/dev/null \
  || echo 'CLAUDE_SMART_USE_LOCAL_EMBEDDING=1' >> ~/.reflexio/.env
# The backend starts one shared embedding daemon on 127.0.0.1:8072.

# 4. (Optional) start the backend manually instead of letting SessionStart spawn it
cd plugin && BACKEND_PORT=8071 EMBEDDING_PORT=8072 uv run --no-sync reflexio services start --only backend --no-reload
# Health check: curl http://localhost:8071/health  →  {"status":"healthy"}
# Stop:         uv run --no-sync reflexio services stop
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
uv run --project plugin --no-sync python -c "import reflexio, os; print(reflexio.__version__); print(os.path.dirname(reflexio.__file__))"
```

### Exercising the install CLIs against a local marketplace

Useful when you're modifying the `install` subcommand itself and want to test without re-publishing:

```bash
uv run --project plugin --no-sync claude-smart install --source $PWD    # Python path
node bin/claude-smart.js install --source $PWD                # Node path
uv run --project plugin --no-sync claude-smart install --host codex      # Codex repo-local path
node bin/claude-smart.js install --host codex                  # Codex packaged-marketplace path
```

The Claude Code install commands accept either a GitHub `owner/repo` ref or an absolute path to a local directory containing `.claude-plugin/marketplace.json`. The Codex Python path creates a local marketplace wrapper that copies this checkout's `plugin/`; the Codex Node path exercises the packaged no-clone flow by copying the bundled plugin into `~/.claude/plugins/marketplaces/reflexioai/plugins/claude-smart` before registering it with Codex. Both Codex paths ask `codex app-server` for the current claude-smart hook hashes and write matching trust state to `~/.codex/config.toml`.

## Tests

```bash
uv run --project plugin pytest --rootdir . -o addopts= tests/ -q   # claude-smart package tests (from repo root)
cd ../reflexio && uv run pytest tests/server/llm/ -q -o 'addopts='   # reflexio patch tests
```

Run both locally before `make release`. There's no CI gate today — the release flow trusts the maintainer.

### Exercising a hook handler directly

Useful for debugging without a live Claude Code session:

```bash
echo '{"session_id":"dev-1","source":"startup","cwd":"'"$PWD"'"}' \
  | uv run --project plugin --no-sync python -m claude_smart.hook session-start
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
