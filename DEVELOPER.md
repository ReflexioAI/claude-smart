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
| `CLAUDE_SMART_CITATION_LINK_STYLE` | `markdown` | Controls the visible links in the final `✨ claude-smart rule applied` marker. `markdown` asks the model to emit `[human title](dashboard URL)` links and is the Codex default because Codex renders markdown links in assistant messages. `osc8` asks it to emit terminal-native OSC 8 hyperlinks so the human title can be clickable without showing the URL; unsupported terminals should fall back to markdown links. |
| `CLAUDE_SMART_DASHBOARD_URL` | `http://localhost:3001` | Base URL used in injected citation targets and the final `✨ claude-smart rule applied` marker links. Override only if the local dashboard is exposed on another host or port. Model-facing citation links use short `/rules/{citationId}` resolver URLs; canonical direct links remain `/skills/project/{id}`, `/skills/shared/{id}`, and `/preferences/project/{id}`. `/preferences/{id}` remains a compatibility alias. |
| `CLAUDE_SMART_CLI_PATH` | `claude` on Claude Code; Codex compatibility wrapper or `codex` on Codex | Override the executable Reflexio calls for local generation. |
| `CLAUDE_SMART_STATE_DIR` | `~/.claude-smart/sessions/` | Where the per-session JSONL buffer lives. |
| `CLAUDE_SMART_NODE_LTS_MAJOR` | `22` | Major version used when the installer downloads private Node.js/npm into `~/.claude-smart/node/current`. |
| `CLAUDE_SMART_NODE_BASE_URL` | `https://nodejs.org/dist/latest-v${CLAUDE_SMART_NODE_LTS_MAJOR}.x` | Override the Node.js download base URL for tests, mirrors, or recovery. |
| `CLAUDE_SMART_BACKEND_AUTOSTART` | `1` | Set to `0` to stop the SessionStart hook from spawning the reflexio backend on `localhost:8071`. |
| `CLAUDE_SMART_DASHBOARD_AUTOSTART` | `1` | Set to `0` to stop the SessionStart hook from spawning the Next.js dashboard on `localhost:3001`. |
| `CLAUDE_SMART_BACKEND_STOP_ON_END` | `0` | Set to `1` to tear down the backend at `SessionEnd` instead of leaving it long-lived. |
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

There is one local test path: build the npm tarball from this checkout and install it. The tarball bundles the plugin (and, in vendor mode, a Reflexio source snapshot), so installing it exercises your local plugin code end-to-end on both hosts.

```bash
make package
# → prints the absolute path of the resulting claude-smart-<version>.tgz
```

Install the tarball globally and run the host installer:

```bash
npm install -g /abs/path/claude-smart-<version>.tgz   # path or ./prefix, not a bare name
claude-smart install                   # Claude Code
claude-smart install --host codex      # Codex
```

npm decides the source from the *shape* of the argument: a filesystem path (absolute, or relative with a `./` prefix) is installed as a local tarball, while a bare `claude-smart-<version>.tgz` is treated as a registry package name and fails. Always pass a path.

Restart the host. `claude-smart install` (Claude Code) registers the bundled package root as a local marketplace and runs `claude plugin install claude-smart@reflexioai`; the Codex variant copies the bundled plugin into Codex's marketplace cache. Either way, your local plugin changes are what gets loaded.

To iterate: edit plugin code, rerun `make package`, reinstall the tarball, rerun `claude-smart install`, restart the host.

### Reinstalling a rebuilt tarball (same name/version)

During active development you'll often rebuild the tarball without bumping the version. A plain `npm install -g` can then be a no-op, because npm sees the version already satisfied. Force the overwrite, or uninstall first:

```bash
npm install -g --force /abs/path/claude-smart-<version>.tgz   # force overwrite
# or, most predictable:
npm uninstall -g claude-smart && npm install -g /abs/path/claude-smart-<version>.tgz
```

Reinstalling the npm package only refreshes the `claude-smart` CLI wrapper — the plugin payload the host loads is whatever `claude-smart install` last copied in, so always re-run `claude-smart install` and restart the host afterward. If a same-version change still doesn't take effect (the host may dedupe the plugin by version), force a clean re-ingest:

```bash
claude plugin uninstall claude-smart@reflexioai   # Claude Code
claude-smart install
```

Do **not** use `claude-smart update` for this loop — it wraps `claude plugin update claude-smart@reflexioai`, the end-user path for pulling a newer *published* release from the marketplace. It is version-driven (a same-version rebuild has nothing to update to) and updates from the registered source rather than re-ingesting a tarball you just built. Bumping the patch version with `make bump` sidesteps all version-dedup ambiguity if you'd rather not use `--force`.

For Reflexio backend changes, edit `open_source/reflexio/` and either:
- Publish `reflexio-ai` to PyPI and bump `plugin/pyproject.toml`, or
- Use vendor mode (see [Release flow with Reflexio](#release-flow-with-reflexio) below) — `make package` then bundles the local Reflexio source into `plugin/vendor/reflexio` so the tarball install picks it up.

What's picked up after each reinstall + host restart:

- `plugin/src/claude_smart/`, `plugin/commands/*.md`, `plugin/hooks/`, `plugin/dashboard/` — all from the freshly installed tarball. There is no editable code path; the running plugin is always whatever the latest installed tarball contains.

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

### Sanity check

Inside Claude Code:

```
/show
```

On a fresh project: `_No skills or preferences yet for project <name>._`. Have a conversation, correct Claude on something (e.g. `"no, don't use X — use Y"`), then run `/learn` — this flags the previous turn as a correction and forces extraction immediately. After ~20–30 seconds, `/show` surfaces the new skill or preference.

To check which Reflexio source is loaded (PyPI vs vendored bundle):

```bash
uv run --project plugin --no-sync python -c "import reflexio, os; print(reflexio.__version__); print(os.path.dirname(reflexio.__file__))"
```

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
