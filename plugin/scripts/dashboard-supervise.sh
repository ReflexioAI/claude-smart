#!/usr/bin/env bash
# Supervisor for the claude-smart Next.js dashboard.
#
# Problem it solves: dashboard-service.sh used to fire `next start` detached and
# forget about it. When that process died later (a crash, or a macOS jetsam
# pressure-kill of next-server), nothing restarted it until the next *real*
# SessionStart hook happened to run `start` again — leaving the dashboard down
# for as long as Claude Code stayed closed. This wrapper respawns next-server so
# a silent death self-heals within seconds.
#
# Lifecycle: dashboard-service.sh spawn_dashboard() launches this detached as the
# session leader (setsid) and records THIS pid in dashboard.pid. `stop`
# group-kills that pid, so the TERM trap below breaks the loop and the
# next-server child (same process group) is torn down with us — an intentional
# stop never triggers a respawn.
#
# Crash-loop guard: a broken build that starts and immediately exits must not be
# respawned forever. Exits faster than HEALTHY_SECS are counted as consecutive
# fast failures; after MAX_FAILS of them the supervisor gives up (exit 1) and
# leaves recovery to the next SessionStart. Any run that stays up at least
# HEALTHY_SECS resets the counter, so a long-lived dashboard that eventually
# dies still self-heals.
#
# Note (deliberate limitation): this is an in-process supervisor. A system-wide
# memory-pressure kill can take the whole process group (supervisor + child)
# down at once, in which case recovery still falls to the next SessionStart. A
# launchd/OS-level supervisor would survive that; it is intentionally left as a
# follow-up.
#
# Tunables (env; defaults chosen for production, overridden in tests):
#   CLAUDE_SMART_DASHBOARD_RESPAWN_DELAY  seconds to wait between respawns (2)
#   CLAUDE_SMART_DASHBOARD_HEALTHY_SECS   uptime that counts as healthy    (30)
#   CLAUDE_SMART_DASHBOARD_MAX_FAILS      consecutive fast exits allowed    (5)
set -u

NEXT_BIN="${1:?dashboard-supervise.sh: next binary path required}"
PORT="${2:?dashboard-supervise.sh: port required}"

RESPAWN_DELAY="${CLAUDE_SMART_DASHBOARD_RESPAWN_DELAY:-2}"
HEALTHY_SECS="${CLAUDE_SMART_DASHBOARD_HEALTHY_SECS:-30}"
MAX_FAILS="${CLAUDE_SMART_DASHBOARD_MAX_FAILS:-5}"

# On stop (TERM/INT — normally delivered to the whole process group by
# `dashboard-service.sh stop`) tear down the current next-server and exit the
# loop instead of respawning. next-server runs in the background and we `wait`
# on it so the trap fires immediately, even if only this supervisor is signaled.
child=""
on_stop() {
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
  echo "[claude-smart] dashboard supervisor: received stop signal; exiting"
  exit 0
}
trap on_stop TERM INT

fails=0
while true; do
  started="$(date +%s 2>/dev/null || echo 0)"
  "$NEXT_BIN" start -p "$PORT" -H 127.0.0.1 &
  child=$!
  wait "$child"
  code=$?
  ended="$(date +%s 2>/dev/null || echo 0)"

  # 128+15=143: next-server was SIGTERM'd on its own — normally the port reaper
  # in `dashboard-service.sh stop`/reinstall when it could not see this
  # supervisor's pid (e.g. a stop issued from a different HOME). Respect an
  # intentional stop instead of resurrecting the dashboard. Genuine crashes and
  # jetsam SIGKILLs (137) fall through to the respawn path below. (A stop that
  # *does* see our pid group-kills this supervisor, handled by the trap above.)
  if [ "$code" -eq 143 ]; then
    echo "[claude-smart] dashboard supervisor: next-server received SIGTERM (intentional stop); exiting without respawn"
    exit 0
  fi

  if [ "$((ended - started))" -ge "$HEALTHY_SECS" ]; then
    fails=0
  else
    fails=$((fails + 1))
  fi

  if [ "$fails" -ge "$MAX_FAILS" ]; then
    echo "[claude-smart] dashboard supervisor: next-server exited (code $code); ${MAX_FAILS} fast failures in a row — giving up (recovery deferred to next SessionStart)"
    exit 1
  fi

  echo "[claude-smart] dashboard supervisor: next-server exited (code $code); respawning in ${RESPAWN_DELAY}s (consecutive fast failures: $fails)"
  sleep "$RESPAWN_DELAY"
done
