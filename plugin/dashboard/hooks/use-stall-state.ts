"use client";

import { useEffect, useState } from "react";

const POLL_INTERVAL_MS = 60_000;

export type StallReason = "billing_error" | "auth_error";

export interface StallState {
  stalled: boolean;
  reason: StallReason | null;
  stalled_at: string | null;
  reset_estimate: string | null;
  notified_in_cc: boolean;
  error_message: string | null;
}

/**
 * Polls reflexio's GET /stall_state every minute. Returns the latest
 * snapshot or `null` while still loading / when the server is unreachable.
 */
export function useStallState(): StallState | null {
  const [state, setState] = useState<StallState | null>(null);

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10_000);
      try {
        const resp = await fetch("/api/reflexio/stall_state", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!resp.ok) return;
        const body: StallState = await resp.json();
        if (!cancelled) setState(body);
      } catch {
        // Reflexio offline / aborted — leave previous state in place.
      } finally {
        clearTimeout(timer);
      }
    };

    tick();
    const id = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return state;
}
