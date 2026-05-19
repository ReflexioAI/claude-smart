"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Users, ChevronRight } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { EmptyState } from "@/components/common/empty-state";
import { DeleteAllButton } from "@/components/common/delete-all-button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { reflexio } from "@/lib/reflexio-client";
import { useSettings } from "@/hooks/use-settings";
import { formatRelative, truncateId } from "@/lib/format";
import type { UserProfile } from "@/lib/types";

export default function PreferencesPage() {
  const { reflexioUrl } = useSettings();
  const [profiles, setProfiles] = useState<UserProfile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    reflexio
      .getAllProfiles({ reflexioUrl })
      .then((res) => {
        if (!cancelled) {
          setProfiles(res.user_profiles ?? []);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reflexioUrl]);

  const filtered = (profiles ?? []).filter(
    (p) =>
      p.content.toLowerCase().includes(filter.toLowerCase()) ||
      p.user_id.toLowerCase().includes(filter.toLowerCase()),
  );

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Preferences"
        description="Project-scoped preferences extracted from interactions."
        actions={
          <div className="flex items-center gap-2">
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
                await reflexio.deleteAllProfiles(reflexioUrl);
                setProfiles([]);
              }}
            />
          </div>
        }
      />

      <div className="p-6">
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm mb-4">
            {error}. Is reflexio running on the URL in the top bar?
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
          <div className="grid gap-3 md:grid-cols-2">
            {filtered.map((p) => (
              <Link
                key={p.profile_id}
                href={`/preferences/project/${encodeURIComponent(p.profile_id)}`}
                className="group block rounded-lg border border-border bg-card/92 p-4 shadow-sm transition-colors hover:border-primary/35 hover:bg-accent/45"
              >
                <header className="flex items-center justify-between gap-2 mb-2">
                  <div className="flex items-center gap-2 min-w-0">
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
                <p className="rounded-md border border-border bg-background/60 px-3 py-2 text-sm leading-relaxed line-clamp-4">
                  {p.content}
                </p>
                {p.source && (
                  <p className="text-[11px] text-muted-foreground mt-2 font-mono">
                    source: {p.source}
                  </p>
                )}
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
