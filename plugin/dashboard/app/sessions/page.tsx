"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { MessageSquare, Search } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { EmptyState } from "@/components/common/empty-state";
import { DeleteAllButton } from "@/components/common/delete-all-button";
import { LearningsBadge } from "@/components/common/learnings-badge";
import { Input } from "@/components/ui/input";
import { dayBucket, formatRelative, truncateId } from "@/lib/format";
import type { SessionSummary } from "@/lib/types";

export default function SessionsPage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/sessions", { cache: "no-store" })
      .then((r) => r.json())
      .then((data) => {
        if (!cancelled) setSessions(data.sessions ?? []);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return sessions ?? [];
    return (sessions ?? []).filter((s) => {
      const hay = `${s.session_id} ${s.preview ?? ""}`.toLowerCase();
      return hay.includes(q);
    });
  }, [sessions, filter]);

  const grouped = useMemo(() => {
    const buckets = new Map<string, SessionSummary[]>();
    for (const s of filtered) {
      const key = dayBucket(s.last_activity);
      const arr = buckets.get(key);
      if (arr) arr.push(s);
      else buckets.set(key, [s]);
    }
    return Array.from(buckets.entries());
  }, [filtered]);

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Sessions"
        description="Sessions buffered locally under ~/.claude-smart/sessions/. Each row is one session; click to see its interactions."
        actions={
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Search sessions…"
                className="h-8 w-64 pl-7 text-xs"
              />
            </div>
            <DeleteAllButton
              label={`Delete all${sessions && sessions.length > 0 ? ` (${sessions.length})` : ""}`}
              confirmMessage={`Delete ALL ${sessions?.length ?? 0} local session buffers? This cannot be undone.`}
              disabled={!sessions || sessions.length === 0}
              onConfirm={async () => {
                const res = await fetch("/api/sessions", { method: "DELETE" });
                if (!res.ok) throw new Error(`delete failed: ${res.status}`);
                setSessions([]);
              }}
            />
          </div>
        }
      />

      <div className="p-6">
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm mb-4">
            {error}
          </div>
        )}

        {sessions === null && !error ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : sessions && sessions.length > 0 && filtered.length === 0 ? (
          <EmptyState
            icon={Search}
            title="No matches"
            description={`No session matches "${filter}".`}
          />
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={MessageSquare}
            title="No sessions found"
            description="Start Claude Code with claude-smart enabled — JSONL buffers will appear in ~/.claude-smart/sessions/."
          />
        ) : (
          <div className="space-y-6">
            {grouped.map(([label, items]) => (
              <section key={label}>
                <div className="flex items-center gap-2 mb-2 px-1">
                  <h2 className="text-[11px] uppercase tracking-wider font-medium text-muted-foreground">
                    {label}
                  </h2>
                  <span className="text-[11px] text-muted-foreground/70">
                    · {items.length}
                  </span>
                </div>
                <div className="rounded-xl border border-border divide-y divide-border bg-card overflow-hidden">
                  {items.map((s) => (
                    <div
                      key={s.session_id}
                      role="link"
                      tabIndex={0}
                      onClick={() =>
                        router.push(
                          `/sessions/${encodeURIComponent(s.session_id)}`,
                        )
                      }
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          router.push(
                            `/sessions/${encodeURIComponent(s.session_id)}`,
                          );
                        }
                      }}
                      className="flex items-center gap-3 px-4 py-2.5 cursor-pointer hover:bg-accent/40 focus:bg-accent/40 focus:outline-none transition-colors"
                    >
                      <MessageSquare className="h-4 w-4 text-muted-foreground shrink-0" />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <p className="text-sm truncate">
                            {s.preview ?? (
                              <span className="text-muted-foreground italic">
                                (no user turns yet)
                              </span>
                            )}
                          </p>
                          <LearningsBadge
                            count={s.learning_interaction_count}
                            size="sm"
                          />
                        </div>
                        <div className="flex items-center gap-3 text-[11px] text-muted-foreground mt-0.5">
                          <code className="font-mono">
                            {truncateId(s.session_id, 10, 6)}
                          </code>
                          <span>·</span>
                          <span className="tabular-nums">
                            {s.turn_count} turn{s.turn_count === 1 ? "" : "s"}
                          </span>
                          {s.published_up_to > 0 &&
                            s.published_up_to < s.turn_count && (
                              <>
                                <span>·</span>
                                <span className="tabular-nums">
                                  {s.published_up_to} published
                                </span>
                              </>
                            )}
                        </div>
                      </div>
                      <div className="text-xs text-muted-foreground shrink-0 tabular-nums">
                        {formatRelative(s.last_activity)}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
