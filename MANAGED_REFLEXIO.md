# Managed Reflexio for claude-smart

This guide explains how to run claude-smart against the managed Reflexio
service instead of the local Reflexio backend.

Managed mode is opt-in. If you install claude-smart without an API key, setup
uses the current local backend flow and stores data on your machine. If you pass
an API key, setup writes managed-service credentials to `~/.reflexio/.env` and
hooks send learning data to `https://www.reflexio.ai/`.

## When to Use Managed Mode

Use managed mode when you want:

- Shared Reflexio state across machines.
- No local Reflexio backend process.
- Managed storage and authentication through Reflexio.

Use local mode when you want:

- Fully local storage under `~/.reflexio/`.
- Offline-friendly semantic search and extraction.
- No external Reflexio service dependency.

## Install with Managed Reflexio

Set your managed Reflexio API key in your shell:

```bash
export REFLEXIO_API_KEY="rflx-your-api-key"
```

Install for Claude Code:

```bash
npx claude-smart install --api-key "$REFLEXIO_API_KEY"
```

Install for Codex:

```bash
npx claude-smart install --host codex --api-key "$REFLEXIO_API_KEY"
```

The default managed URL is:

```text
https://www.reflexio.ai/
```

To override it:

```bash
npx claude-smart install \
  --api-key "$REFLEXIO_API_KEY" \
  --reflexio-url "https://www.reflexio.ai/"
```

After install, restart Claude Code or Codex so hooks reload with the new
configuration.

## What Setup Writes

Managed setup writes these values to `~/.reflexio/.env`:

```env
REFLEXIO_URL="https://www.reflexio.ai/"
REFLEXIO_API_KEY="rflx-your-api-key"
```

The file is written with mode `0600`. Unknown keys, comments, and unrelated
settings are preserved.

Local setup still writes only the local-mode flags:

```env
CLAUDE_SMART_USE_LOCAL_CLI=1
CLAUDE_SMART_USE_LOCAL_EMBEDDING=1
```

`REFLEXIO_URL` and `REFLEXIO_API_KEY` are written only when `--api-key` is
provided.

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

The bearer token is attached only when the proxy target matches the configured
`REFLEXIO_URL` origin. If you temporarily point the dashboard endpoint field at
another URL, the proxy can forward the request, but it will not send the managed
API key to that alternate origin.

Open the dashboard the same way as local mode:

- Claude Code: `/claude-smart:dashboard`
- Codex: `bash ~/.reflexio/plugin-root/scripts/dashboard-open.sh`

The dashboard Environment page can view or update `REFLEXIO_URL` and
`REFLEXIO_API_KEY`.

## Citation Links

When claude-smart is using a remote `REFLEXIO_URL`, citation links point to the
managed Reflexio list pages for now:

- Profiles: `https://www.reflexio.ai/profiles`
- Playbooks: `https://www.reflexio.ai/playbooks`

Local mode keeps using local dashboard rule links such as
`http://localhost:3001/rules/...`.

## Switch Back to Local Mode

Edit `~/.reflexio/.env` and remove:

```env
REFLEXIO_URL=...
REFLEXIO_API_KEY=...
```

Then make sure the local flags are present:

```env
CLAUDE_SMART_USE_LOCAL_CLI=1
CLAUDE_SMART_USE_LOCAL_EMBEDDING=1
```

Restart Claude Code or Codex, or run:

```bash
bash ~/.reflexio/plugin-root/scripts/backend-service.sh start
```

Local mode will use the local Reflexio backend at `http://localhost:8071`.

## Troubleshooting

If managed learning does not appear:

- Confirm `REFLEXIO_API_KEY` is present in `~/.reflexio/.env`.
- Confirm `REFLEXIO_URL` points at `https://www.reflexio.ai/`.
- Run the `curl` command above and check for HTTP 200.
- Restart Claude Code or Codex after changing `.env`.
- Check `~/.claude-smart/backend.log` for hook startup messages.

If the local backend starts unexpectedly, check for a localhost URL in
`~/.reflexio/.env`. Any non-local `REFLEXIO_URL` puts claude-smart in managed
mode; an absent or localhost URL uses local mode.
