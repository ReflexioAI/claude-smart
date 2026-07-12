import type { Host, SessionSummary } from "./types";

/** Join project-skill request IDs to host facts recorded in local session JSONL. */
export function hostByRequestId(
  localSessions: SessionSummary[],
): Map<string, Host | null> {
  const result = new Map<string, Host | null>();
  for (const session of localSessions) {
    for (const requestId of session.request_ids) {
      result.set(requestId, session.host);
    }
  }
  return result;
}
