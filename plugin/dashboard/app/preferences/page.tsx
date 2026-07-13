"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Users, ChevronRight } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { HostBadge } from "@/components/common/host-badge";
import { LearningApplicationBadge } from "@/components/common/learning-application-badge";
import { EmptyState } from "@/components/common/empty-state";
import { DeleteAllButton } from "@/components/common/delete-all-button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { reflexio } from "@/lib/reflexio-client";
import { formatRelative, truncateId } from "@/lib/format";
import { useRequestHostAttribution } from "@/lib/host-attribution";
import type { PlaybookApplicationStat, UserProfile } from "@/lib/types";

type PreferenceSort = "newest" | "applied";

function profileStatKey(profile: UserProfile): string {
  return `profile:profile:${profile.profile_id}`;
}

export default function PreferencesPage() {
  const [profiles, setProfiles] = useState<UserProfile[] | null>(null);
  const [appStats, setAppStats] = useState<PlaybookApplicationStat[] | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<PreferenceSort>("newest");
  const [filter, setFilter] = useState("");
  const attribution = useRequestHostAttribution();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [profileRes, statsRes] = await Promise.all([
          reflexio.getAllProfiles(),
          fetch("/api/rules/applied?daysBack=30&limit=200", {
            cache: "no-store",
          })
            .then((r) => r.json())
            .catch(() => ({
              success: false,
              stats: [] as PlaybookApplicationStat[],
            })),
        ]);
        if (cancelled) return;
        setProfiles(profileRes.user_profiles ?? []);
        setAppStats(statsRes.stats ?? []);
        setError(null);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const statsByProfile = useMemo(() => {
    const map = new Map<string, PlaybookApplicationStat>();
    for (const s of appStats ?? []) {
      map.set(`${s.kind}:${s.source_kind ?? "unknown"}:${s.real_id}`, s);
    }
    return map;
  }, [appStats]);

  const filtered = useMemo(() => {
    const query = filter.toLowerCase();
    const matches = (profiles ?? []).filter(
      (p) =>
        p.content.toLowerCase().includes(query) ||
        p.user_id.toLowerCase().includes(query),
    );
    return matches.sort((a, b) => {
      if (sortBy === "applied") {
        const aStat = statsByProfile.get(profileStatKey(a));
        const bStat = statsByProfile.get(profileStatKey(b));
        const appliedDelta =
          (bStat?.applied_count ?? 0) - (aStat?.applied_count ?? 0);
        if (appliedDelta !== 0) return appliedDelta;
        const recencyDelta =
          (bStat?.last_applied_at ?? 0) - (aStat?.last_applied_at ?? 0);
        if (recencyDelta !== 0) return recencyDelta;
      }
      return b.last_modified_timestamp - a.last_modified_timestamp;
    });
  }, [filter, profiles, sortBy, statsByProfile]);

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Preferences"
        description="Project-scoped preferences extracted from interactions."
        actions={
          <div className="flex max-w-full flex-wrap items-center justify-end gap-2">
            <Select
              value={sortBy}
              onValueChange={(v) => setSortBy((v as PreferenceSort) ?? "newest")}
            >
              <SelectTrigger size="sm" className="w-36 text-xs bg-background/80">
                <SelectValue placeholder="Sort">
                  {sortBy === "applied" ? "Most applied" : "Newest"}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="newest">Newest</SelectItem>
                <SelectItem value="applied">Most applied</SelectItem>
              </SelectContent>
            </Select>
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter"
              className="h-9 w-56 text-xs bg-background/80"
            />
            <DeleteAllButton
              label={`Delete all${profiles && profiles.length > 0 ? ` (${profiles.length})` : ""}`}
              confirmMessage={`Delete ALL ${profiles?.length ?? 0} preferences? Preferences regenerate from fresh interactions, but this cannot be undone.`}
              disabled={!profiles || profiles.length === 0}
              onConfirm={async () => {
                await reflexio.deleteAllProfiles();
                setProfiles([]);
              }}
            />
          </div>
        }
      />

      <div className="p-6">
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm mb-4">
            {error}. Is reflexio running on the configured backend URL?
          </div>
        )}
        {attribution?.unavailable && !error && (
          <div className="mb-4 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-200">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            Preference source details are temporarily unavailable. Preferences
            remain available.
          </div>
        )}

        {profiles === null && !error ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={Users}
            title="No preferences yet"
            description="Keep using Claude with claude-smart enabled — preferences will appear here automatically as the extractor learns patterns from your interactions."
          />
        ) : (
          <div className="overflow-hidden rounded-lg border border-border bg-card/92 shadow-sm">
            {filtered.map((p) => {
              const stat = statsByProfile.get(profileStatKey(p));
              return (
                <Link
                  key={p.profile_id}
                  href={`/preferences/project/${encodeURIComponent(p.profile_id)}`}
                  className="group block border-b border-border px-4 py-3.5 transition-colors last:border-b-0 hover:bg-accent/35"
                >
                  <header className="mb-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <LearningApplicationBadge stat={stat} />
                      <Badge variant="outline" className="h-5 font-mono text-[10px]">
                        {truncateId(p.user_id, 32, 8)}
                      </Badge>
                      {p.status && (
                        <Badge variant="secondary" className="h-5 text-[10px]">
                          {p.status}
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <span className="text-[11px] text-muted-foreground">
                        {formatRelative(p.last_modified_timestamp)}
                      </span>
                      <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/60 group-hover:text-foreground transition-colors" />
                    </div>
                  </header>
                  <p className="max-w-5xl text-sm leading-relaxed line-clamp-3">
                    {p.content}
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    {attribution &&
                      !attribution.unavailable &&
                      p.generated_from_request_id && (
                        <HostBadge
                          host={
                            attribution.hosts.get(
                              p.generated_from_request_id,
                            ) ?? null
                          }
                          display="provenance"
                        />
                      )}
                    {p.source && (
                      <span className="text-[11px] text-muted-foreground font-mono">
                        Integration: {p.source}
                      </span>
                    )}
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
