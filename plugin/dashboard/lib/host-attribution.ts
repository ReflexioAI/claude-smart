import type { Host, SessionSummary } from "./types";

export function hostsByRequestId(
  sessions: SessionSummary[],
): Map<string, Host | null> {
  const result = new Map<string, Host | null>();
  for (const session of sessions) {
    for (const requestId of session.request_ids) {
      result.set(requestId, session.host);
    }
  }
  return result;
}
