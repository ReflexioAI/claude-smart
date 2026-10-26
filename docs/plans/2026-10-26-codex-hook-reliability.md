# Codex hook deadlines and durable publishing

## Scope

Implement the approved reliability recommendation against Claude Smart
`aa411d4` in an isolated feature checkout. Do not change an installed cache,
release a package, restart services or replay real session content. HTTP 402
and logical backend rejection remain real failures; changing timeouts cannot
make an unsuccessful learning update succeed.

## Invariants

- Codex command hooks finish inside their existing 15/30-second host limits.
  Python environment preparation belongs to the installer, not a live hook.
- A saved turn and its response identity remain durable before any background
  publication. Enqueue is not a remote success and does not move a watermark.
- The existing session JSONL, frozen publish ranges, request IDs, evidence and
  lost-acknowledgement verification remain the publication source of truth.
- One shared worker owner bounds background publication concurrency. It holds
  ownership until its actual work finishes. No per-hook executor pools.
- Workers publish only explicitly queued work for their bound backend/account,
  respecting read-only mode and config changes. No credentials or conversation
  content in queue metadata; no scanning/rebinding unrelated legacy buffers.
- A racing enqueue cannot be removed by an older worker generation. Failures,
  process death and unavailable workers leave recoverable work, not success.
- Logging and diagnostics never hold up a hook on a contended lock. Diagnostics
  contain event/stage/timing/outcome metadata, not prompts, tool output or keys.

## Implementation

1. **Launcher:** use the installer's `.venv/bin/python` or Windows
   `.venv/Scripts/python.exe` directly. If missing, schedule the existing
   installer recovery and return a visible degraded outcome without inline
   dependency resolution. Bound the Python subprocess below the host timeout;
   record startup, timeout, exit and duration separately from handler logs.
2. **Capture and publication:** Codex Stop journals its assistant turn, creates
   a private atomic outbox marker and kicks a detached prepared-Python worker.
   The worker reuses publication primitives, serializes draining, respects
   destination/read-only admission and retains pending work after errors.
   Recover explicit queued work at session startup and later Stop calls.
   Avoid full transcript reads when Codex supplies its final assistant message,
   preserving internal/title filtering and the fallback for missing text.
3. **Logging and timing:** make the existing log lock nonblocking. Add elapsed
   handler time and outcome fields; the launcher supplies evidence even when
   Python never starts or cannot reach its completion log.
4. **Verification:** exercise actual Node-to-Python dispatch with isolated fake
   runtimes, missing environments, hung children, slow adapters, duplicate
   publishers, lost ACKs, worker crashes, racing enqueues, account switching,
   read-only mode and log contention. Use temporary state and fake/local
   transport only. Run the complete plugin test suite after integration and
   independently review the complete diff.

## Dependent artifacts

Runtime: `plugin/scripts/codex-hook.js`, Python dispatcher/logging,
`events/stop.py`, startup recovery, publisher/state and the new outbox worker.
Tests: Codex launcher/integration, Stop, publication, queue ownership/recovery,
logging, and the existing cross-host suite. Documentation: this plan,
`README.md`, `plugin/README.md`, `plugin/src/README.md`, `TROUBLESHOOTING.md`
and relevant architecture descriptions. Hook JSON timeouts remain compatible;
no new user configuration flags or renamed commands are needed. Inspect skills,
installer/restart entrypoints and packaging to ensure the worker ships and
manual commands continue to work; do not edit downloaded installed skills.

This is one compatible source change, not a competing runtime or an installation
migration. Existing buffer formats and synchronous non-Codex callers remain
supported. A later reviewed release/install is required to change live sessions.

## Review and verification

Independent review found two recovery cases, both fixed with regressions:
manual publication can advance beyond an old intent's bound, and a delayed
child must not steal a newer launch claim and strand healthy continuation work.
The reviewer rechecked both fixes and reported no remaining blockers.

Ruff lint/format, Pyright (zero errors/warnings), Node syntax and npm package
inclusion checks passed. The real Node-to-Python Stop test confirms the hook
returns before a blocked fake transport acknowledgement, then the detached
worker advances the watermark only after acknowledgement. The hung-child test
confirms SIGTERM resistance cannot extend the Python subprocess beyond 12s.

The standalone plugin is not linked to TestSprite and the CLI is unavailable;
no TestSprite or installed-plugin smoke test was run. Tests use private temporary
homes/state and fake transport, never live accounts or paid model calls. Python
3.12 matches this repository's CI. An initial 3.14 run exposed an existing
unrelated symlink-loop expectation; runtime compatibility changes are outside
this fix. The initial full run also identified installer fixtures that mocked
`uv`; those now supply prepared Python while preserving provider assertions.

Final full-suite command, using the prepared Python 3.12 environment:

```sh
uv run --project plugin --no-sync pytest -q -o addopts= tests
```

Result: **1,184 passed in 397.93 seconds**. Two existing tar extraction
deprecation warnings remain in packaging tests. All changed Python files pass
Ruff lint/format; changed runtime modules pass Pyright with zero errors/warnings.
Node syntax, package inclusion and `git diff --check` pass. At this implementation
checkpoint, the original enterprise checkout and installed plugin were unchanged;
no commit, push, PR, release, installation or live-service mutation had occurred.

The user subsequently authorized PR creation, local Codex review through P0/P1
clearance, and merge after CI passes. PR #200 rebases the implementation onto
`cedc05f` and refreshes the locked vendor bundle and validation against that base.
Release and installation remain outside this PR's scope.

The first local Codex PR review found two P2 recovery defects: failed-generation
exclusions were lost across worker continuations, and enqueue could replace a
held environment digest. Both were reproduced by new failing regression tests,
then fixed: exclusions travel in the private continuation claim and coalescing
requires the original environment digest. Both regressions pass afterward.
