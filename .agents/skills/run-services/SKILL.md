---
name: run-services
description: Start all local services (backend, frontend, docs) with dependency checking, error diagnosis, and automatic recovery. Handles missing npm/python packages, port conflicts, and stale processes.
---

# Run Services

Start all local development services with pre-flight dependency checks and automatic error recovery.

## Overview

This command automates the full startup workflow:
0. Smart port selection — auto-detects free port group or reuses current worktree's ports
1. Pre-flight dependency checks (uv sync, venv, npm, worktree editable packages)
2. Stops any existing services (only if needed)
3. Starts all services via `run_services.sh`
4. Health-checks each service
5. Diagnoses and fixes failures, then retries (up to 2 retries)

## Port Configuration

Ports are allocated in groups of 3 with a +10 offset between groups:

| Group | Frontend | Backend | Docs |
|-------|----------|---------|------|
| Default | 8080 | 8081 | 8082 |
| +10 | 8090 | 8091 | 8092 |
| +20 | 8100 | 8101 | 8102 |
| +30 | 8110 | 8111 | 8112 |
| +40 | 8120 | 8121 | 8122 |

| Service | Env Var | Base Port |
|---------|---------|-----------|
| Frontend (Next.js) | `FRONTEND_PORT` | 8080 |
| Backend (FastAPI) | `BACKEND_PORT` | 8081 |
| Docs (Fumadocs) | `DOCS_PORT` | 8082 |
Step 0 (Smart Port Selection) determines which group to use automatically, unless ports are already set via environment variables.

## Execution Steps

### Step 0: Smart Port Selection

**Pre-check:** If `BACKEND_PORT`, `FRONTEND_PORT`, or `DOCS_PORT` are already set in the environment, skip auto-detection entirely and use those values directly. Report "Using pre-configured ports" and proceed to Step 1.

**0.1** Get current worktree root:
```bash
WORKTREE_ROOT=$(git rev-parse --show-toplevel)
```

**0.2** For each port group (offset 0, 10, 20, 30, 40 — max 5 attempts), check all 3 ports in the group. For each port, determine its status:

```bash
PID=$(lsof -t -i:$PORT 2>/dev/null | head -1)
```

If no PID → port is **free**.

If PID exists, get the process's working directory:
```bash
PROC_CWD=$(lsof -a -p $PID -d cwd -Fn 2>/dev/null | tail -1 | sed 's/^n//')
```

Then classify:
- **own** — `$PROC_CWD` starts with `$WORKTREE_ROOT` (use prefix match, since child processes like Next.js run from subdirectories like `$WORKTREE_ROOT/reflexio/website`)
- **other** — `$PROC_CWD` does NOT start with `$WORKTREE_ROOT`

**0.3** Decision per group:
- All 3 ports are **free** or **own** → **use this group**. Any "own" ports will be restarted in Step 3.
- Any port is **other** → **skip this group**, try the next offset (+10)
- All 5 groups exhausted → report error: "All port groups (8080-8122) are occupied by other worktrees. Free some ports or set BACKEND_PORT/FRONTEND_PORT/DOCS_PORT manually."

**0.4** Export the chosen ports:
```bash
export FRONTEND_PORT=<8080+N>
export BACKEND_PORT=<8081+N>
export DOCS_PORT=<8082+N>
export API_BACKEND_URL="http://localhost:${BACKEND_PORT}"
```

Report which group was selected and why, e.g.:
- "Using default ports (8080/8081/8082) — all free"
- "Using default ports (8080/8081/8082) — restarting own services"
- "Using offset +10 ports (8090/8091/8092) — default ports occupied by another worktree"

Also record whether any ports in the chosen group were "own" (needs stop) or all "free" (skip stop).

### Step 1: Pre-flight Dependency Checks

Run these checks before starting anything. They are idempotent and fast when deps are already installed.

**Python dependencies (run first — creates `.venv` if missing):**
```bash
uv sync
```

**Activate virtual environment (after `uv sync` so `.venv` exists):**
```bash
source .venv/bin/activate
```

**Worktree editable packages:**
In a git worktree, `uv sync` may resolve `reflexio_commons` and `reflexio_client` to a different worktree's path. Check and fix:
```bash
python -c "import reflexio_commons; import os; assert os.path.abspath(reflexio_commons.__file__).startswith(os.path.abspath('.'))" 2>/dev/null || uv pip install -e reflexio/reflexio_commons -e reflexio/reflexio_client
```

**Frontend dependencies (reflexio/website):**
Check if `node_modules` exists and has content. If missing or empty, install:
```bash
ls reflexio/website/node_modules/.package-lock.json 2>/dev/null || (cd reflexio/website && npm install)
```

**Docs dependencies (reflexio/public_docs):**
Check if `node_modules` exists and has content. If missing or empty, install:
```bash
ls reflexio/public_docs/node_modules/.package-lock.json 2>/dev/null || (cd reflexio/public_docs && npm install)
```

### Step 2: Stop Existing Services (Conditional)

**If any ports in the chosen group were classified as "own" in Step 0:** stop existing services first:
```bash
./stop_services.sh
```
Wait 2 seconds for ports to fully release.

**If all ports were "free":** skip this step entirely — nothing to stop.

### Step 3: Start Services

Run `run_services.sh` in the background with the exported port variables:
```bash
FRONTEND_PORT=$FRONTEND_PORT BACKEND_PORT=$BACKEND_PORT DOCS_PORT=$DOCS_PORT ./run_services.sh > /tmp/reflexio-services.log 2>&1 &
```

Wait ~15 seconds for services to boot. Next.js compilation takes time on first request.

### Step 4: Health Check Each Service

Check each service individually. Use `curl --max-time 10 -s -o /dev/null -w "%{http_code}"` to get HTTP status codes.

**Backend:**
```bash
curl --max-time 10 -s -o /dev/null -w "%{http_code}" http://localhost:${BACKEND_PORT}/health
```
Expected: 200

**Frontend:**
```bash
curl --max-time 10 -s -o /dev/null -w "%{http_code}" http://localhost:${FRONTEND_PORT}
```
Expected: 200 or 3xx (redirect is OK)

**Docs:**
```bash
curl --max-time 10 -s -o /dev/null -w "%{http_code}" http://localhost:${DOCS_PORT}
```
Expected: 200 or 3xx (redirect is OK)

A status of `000` means the service is not responding at all.

### Step 5: On Failure — Diagnose and Fix

If any health check fails, read the log for error details:
```bash
cat /tmp/reflexio-services.log
```

Also check if processes are even running:
```bash
lsof -i:${BACKEND_PORT} -i:${FRONTEND_PORT} -i:${DOCS_PORT}
```

#### Common Failure Patterns and Fixes

**a. "Cannot find package" / "Cannot find module" (npm)**
An npm dependency is missing. Fix:
```bash
# Identify which service (website or public_docs) from the error path
cd reflexio/website && npm install   # or reflexio/public_docs
```
Then retry from Step 3.

**b. "ModuleNotFoundError" / "ImportError" (Python — general)**
A Python dependency is missing. Fix:
```bash
uv sync
```
Then retry from Step 3.

**b2. "ModuleNotFoundError" for `reflexio_commons` or `reflexio_client` (worktree path mismatch)**
In a worktree, editable packages may resolve to a different worktree's path. Fix:
```bash
uv pip install -e reflexio/reflexio_commons -e reflexio/reflexio_client
```
Then retry from Step 3.

**c. "Address already in use" / "EADDRINUSE"**
A port is still occupied. Fix:
```bash
# Kill whatever is on the port
lsof -t -i:PORT | xargs kill -9 2>/dev/null
```
Wait 2 seconds, then retry from Step 3.

**d. ".next build cache errors" / "ENOENT .next"**
Stale Next.js build artifacts. Fix:
```bash
rm -rf reflexio/website/.next reflexio/public_docs/.next
```
Then retry from Step 3.

**e. Script syntax errors or unknown errors**
Report the error output to the user and suggest manual steps. Do not retry.

#### Retry Logic

- Maximum 2 retries after applying a fix
- Each retry starts from Step 3 (stop, start, health check)
- If all retries exhausted, report final status with error details

### Step 6: Report Final Status

Report a summary table:

```
Service    | Port  | Status
-----------|-------|-------
Backend    | 8091  | Running
Frontend   | 8090  | Running
Docs       | 8092  | Running

Port group: offset +10 (8090/8091/8092) — default ports occupied by another worktree
```

Include the port group selection reason in the summary (e.g., "all free", "restarting own services", "default ports occupied by another worktree").

If all services are running, confirm success. If any failed after retries, show:
- The specific error message from logs
- Suggested manual debugging steps

## Important Notes

- **Do NOT modify `.env` files** — port overrides come from shell environment variables only
- **Logs location**: `/tmp/reflexio-services.log` contains combined service output
- **Smart port selection**: Ports are auto-detected unless `BACKEND_PORT`, `FRONTEND_PORT`, or `DOCS_PORT` env vars are already set
- If the user mentions a specific service to start (e.g., "just start the backend"), adapt the workflow to only start/check that service
