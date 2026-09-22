# Managed Reflexio for claude-smart

This guide explains how to run claude-smart against the managed Reflexio
service instead of the local Reflexio backend.

Default installs stay local and need no managed setup:

```bash
npx claude-smart install
```

Use the setup flow for non-local configuration: managed Reflexio, read-only
managed installs, global sharing, or switching an existing managed setup back to
local mode.

```bash
npx claude-smart setup
```

## Where the Settings Live

Setup and install read and write `~/.claude-smart/.env`, the same file the
claude-smart hooks and backend read. `~/.reflexio/.env` belongs to Reflexio
itself (and any other Reflexio tool on the machine) and is never written.

Older releases kept managed settings in `~/.reflexio/.env`. On the next
`npx claude-smart install` or `update`, a managed setup found there is copied
into `~/.claude-smart/.env` once, and the installer prints
`Migrated managed Reflexio settings from …`. A loopback `REFLEXIO_URL` there is
treated as another local Reflexio server's config and is not migrated. The
installer records that it has checked (`~/.claude-smart/legacy-reflexio-env-checked`)
and never reads the legacy file again, so choosing local mode in
`npx claude-smart setup` stays local.

The installer's summary comes from the same file: it prints either
`Using managed Reflexio at <url>` or `Using local Reflexio backend at <url>`,
and only reports a backend or dashboard as running after an HTTP probe answers.

## When to Use Managed Mode

Use managed mode when you want:

- Shared Reflexio state across machines.
- No local Reflexio backend process.
- Managed storage and authentication through Reflexio.

Use local mode when you want:

- Fully local storage under `~/.reflexio/`.
- Offline-friendly semantic search and extraction.
- No external Reflexio service dependency.

## Non-Local Setup

Run the interactive setup command:

```bash
npx claude-smart setup
```

The script prompts for:

- Host: Claude Code, Codex, or both.
- Mode: choose `managed Reflexio` for remote/non-local setup.
- Reflexio API key.
- Read-only mode: choose `yes` to read existing managed skills without
  publishing local interactions.
- Sharing scope: choose `project` for the default project-scoped identity, or
  `global` to share skills across projects.

After collecting those values, setup rewrites the claude-smart entries in
`~/.claude-smart/.env` and then installs or updates the selected host. Restart
Claude Code or fully quit and reopen Codex so the installed hooks reload.

Do not pass managed options to `npx claude-smart install`; install reads the
env file written by setup.

## Setup Options

### Host

The host prompt controls where the plugin is installed after the env file is
written.

Choose `Claude Code` when you use Anthropic's local Claude Code app and want the
Claude Code plugin installed or updated. Setup runs the normal Claude Code
install path after writing `~/.claude-smart/.env`.

Choose `Codex` when you use Codex locally and want the Codex plugin installed
or updated. Setup runs the Codex install path and prepares Codex hooks.

Choose `both` when you use both hosts on the same machine. Both hosts read the
same `~/.claude-smart/.env`, so one managed configuration applies to both Claude
Code and Codex.

### Mode

Choose `managed Reflexio` for non-local setup. Managed mode writes a remote
`REFLEXIO_URL` and `REFLEXIO_API_KEY`, removes local-only claude-smart provider
flags, and makes hooks read and publish through the managed Reflexio service.

Choose `local` only when you want to switch back to local storage and local
backend behavior. Local mode removes the managed keys from
`~/.claude-smart/.env` and writes the local-provider defaults.

### Reflexio API Key

Managed mode requires a Reflexio API key. Setup writes it as:

```env
REFLEXIO_API_KEY="rflx-your-api-key"
```

The key must be non-empty and cannot contain whitespace. Setup validates that
locally and does not call the network, so the flow works even before you have
verified connectivity.

On rerun, setup shows a masked default with only the last four characters
visible. Press Enter to keep the current key. If you paste a real key visibly in
a terminal recording, chat, or shared shell, rotate it afterward.

### Read-Only Mode

Choose `yes` for read-only mode when this machine should use managed skills but
should not publish local interaction data. Setup writes:

```env
CLAUDE_SMART_READ_ONLY="1"
```

During install or update, claude-smart prunes the hooks that publish
interactions. The assistant can still retrieve existing managed profiles,
project skills, and shared skills.

Choose `no` when this machine should both read from managed Reflexio and publish
new learning data back to it. Setup removes `CLAUDE_SMART_READ_ONLY` from
`~/.claude-smart/.env`.

### Sharing Scope

Choose `project` for the default scoped behavior. Setup removes
`REFLEXIO_USER_ID`, so claude-smart computes the Reflexio identity from the
current project, normally from the current git project name. Everyone using the
same managed project identity reads and publishes the same project-scoped
skills, while unrelated projects remain separate.

Choose `global` when you intentionally want skills shared across projects.
Setup writes:

```env
REFLEXIO_USER_ID="global_user"
```

With global sharing, all projects on this machine use the same Reflexio
identity. That is useful for broad personal or team conventions that should
apply everywhere, but it also means project-specific habits can become visible
across projects if you teach them while global sharing is enabled.

## What Setup Writes

Managed setup cleans any previous claude-smart local or managed entries, then
writes the current remote settings to `~/.claude-smart/.env`:

```env
REFLEXIO_URL="https://www.reflexio.ai/"
REFLEXIO_API_KEY="rflx-your-api-key"
```

Depending on prompt choices, it may also write:

```env
CLAUDE_SMART_READ_ONLY="1"
REFLEXIO_USER_ID="global_user"
```

The file is written with mode `0600`. Unknown keys, comments, and unrelated
settings are preserved.

When switching from local mode to managed mode, setup removes local-only flags:

```env
CLAUDE_SMART_USE_LOCAL_CLI=...
CLAUDE_SMART_USE_LOCAL_EMBEDDING=...
```

If an existing `REFLEXIO_URL` points at a loopback host (`localhost`,
`127.0.0.1`, `0.0.0.0`, `[::1]`, over `http` or `https`), setup replaces it
with the managed Reflexio URL.

Local setup removes managed keys from `~/.claude-smart/.env`:

```env
REFLEXIO_URL=...
REFLEXIO_API_KEY=...
REFLEXIO_USER_ID=...
CLAUDE_SMART_READ_ONLY=...
```

## Re-Run Behavior

`npx claude-smart setup` is safe to rerun. It reads the current
`~/.claude-smart/.env`, uses existing values as prompt defaults, and masks existing
API keys by showing only the last four characters.

Press Enter to keep an existing value. Switching from managed to local removes
managed keys. Switching from local to managed removes local-only flags, writes
only the managed entries for the selected scope, and then installs or updates
the selected host.

Setup validates only that the API key is non-empty and contains no whitespace.
It does not call the network, so setup remains offline-capable.

## Verify Managed Access

Use a harmless read endpoint to verify the API key:

```bash
curl -fsS \
  -H "User-Agent: claude-smart" \
  -H "Authorization: Bearer $REFLEXIO_API_KEY" \
  https://www.reflexio.ai/api/whoami
```

A successful response returns JSON describing the authenticated organization and
storage routing.

You can also check claude-smart's backend status:

```bash
bash ~/.reflexio/plugin-root/scripts/backend-service.sh status
```

In managed mode, it should report the remote `REFLEXIO_URL` instead of starting
or requiring the local backend on `http://localhost:8071`.

## Dashboard Behavior

The claude-smart dashboard still runs locally. In managed mode, its Reflexio API
proxy forwards requests to the configured `REFLEXIO_URL` and includes:

```text
Authorization: Bearer <REFLEXIO_API_KEY>
User-Agent: claude-smart
```

Open the dashboard the same way as local mode:

- Claude Code: `/claude-smart:dashboard`
- Codex: `bash ~/.reflexio/plugin-root/scripts/dashboard-open.sh`

When managed citations include stored Reflexio IDs, claude-smart links directly
to the managed Reflexio profile or playbook page, for example
`https://www.reflexio.ai/profiles?profile_id=...` or
`https://www.reflexio.ai/playbooks?resource=user_playbook&user_playbook_id=...`.
Items without stored IDs fall back to the managed list page.

## Troubleshooting

If `npx claude-smart setup` prints `unknown command 'setup'`, the machine is
running an older cached, global, or local claude-smart wrapper. The published
`claude-smart` package includes setup; force npm to resolve that package
instead of reusing an existing wrapper:

```bash
npx --yes claude-smart setup
```

Or use `npm exec` explicitly:

```bash
npm exec --yes --package claude-smart -- claude-smart setup
```

If either command still reaches the stale wrapper, remove the global install
and npx cache, then rerun the pinned command:

```bash
npm uninstall -g claude-smart || true
rm -rf ~/.npm/_npx
hash -r
npx --yes claude-smart setup
```

To confirm which wrapper npm is resolving, run:

```bash
npx --yes claude-smart --help
```

The help output should include:

```text
npx claude-smart setup
```

Local flags an older wrapper left in `~/.reflexio/.env` no longer affect
claude-smart. Rerunning setup in managed mode removes local-only entries from
`~/.claude-smart/.env`.

If managed learning does not appear:

- Confirm `REFLEXIO_API_KEY` is present in `~/.claude-smart/.env`.
- Confirm `REFLEXIO_URL` points at `https://www.reflexio.ai/`.
- Run the `curl` command above and check for HTTP 200.
- Restart Claude Code or Codex after changing `.env`.
- Check `~/.claude-smart/backend.log` for hook startup messages.

If the local backend starts unexpectedly, rerun:

```bash
npx claude-smart setup
```

Choose managed mode and confirm the API key. If you recently updated setup, also
make sure the active plugin was updated and the host app was restarted; changing
`.env` alone cannot activate managed behavior in a stale plugin copy.
