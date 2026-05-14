"use client";

import { useStallState } from "@/hooks/use-stall-state";

/**
 * Persistent top-of-page banner shown while reflexio learning is stalled.
 * Renders nothing when state is null or clean.
 */
export function StallBanner() {
  const state = useStallState();
  if (!state || !state.stalled) return null;

  const text = renderText(state.reason, state.reset_estimate);
  if (!text) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="w-full bg-amber-100 text-amber-900 border-b border-amber-300 px-4 py-2 text-sm dark:bg-amber-950 dark:text-amber-100 dark:border-amber-800"
    >
      {text}
    </div>
  );
}

function renderText(
  reason: string | null,
  resetEstimate: string | null,
): string {
  if (reason === "billing_error") {
    if (resetEstimate) {
      const formatted = formatReset(resetEstimate);
      return `claude-smart: learning paused — Agent SDK credit exhausted (resets ~${formatted}).`;
    }
    return "claude-smart: learning paused — Agent SDK credit exhausted.";
  }
  if (reason === "auth_error") {
    return "claude-smart: learning paused — please run /login in Claude Code.";
  }
  return "";
}

function formatReset(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
