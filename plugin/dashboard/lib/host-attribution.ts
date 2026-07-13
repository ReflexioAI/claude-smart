import { useEffect, useState } from "react";
import type { Host, SessionSummary } from "./types";

export interface RequestHostAttribution {
  hosts: ReadonlyMap<string, Host | null>;
  unavailable: boolean;
}

function hostsByRequestId(
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

export function useRequestHostAttribution(): RequestHostAttribution | null {
  const [attribution, setAttribution] =
    useState<RequestHostAttribution | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5_000);

    fetch("/api/sessions", {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`sessions ${response.status}`);
        const data = await response.json();
        return (data.sessions ?? []) as SessionSummary[];
      })
      .then((sessions) => {
        if (!cancelled) {
          setAttribution({
            hosts: hostsByRequestId(sessions),
            unavailable: false,
          });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAttribution({ hosts: new Map(), unavailable: true });
        }
      })
      .finally(() => clearTimeout(timeout));

    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timeout);
    };
  }, []);

  return attribution;
}
