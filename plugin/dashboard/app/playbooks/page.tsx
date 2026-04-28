"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { BookOpen } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
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
import { useSettings } from "@/hooks/use-settings";
import { formatRelative } from "@/lib/format";
import { statusLabel } from "@/lib/status";
import type { UserPlaybook } from "@/lib/types";

export default function PlaybooksPage() {
  const { reflexioUrl } = useSettings();
  const [playbooks, setPlaybooks] = useState<UserPlaybook[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [agentVersion, setAgentVersion] = useState<string>("__all__");
  const [statusFilter, setStatusFilter] = useState<string>("CURRENT");
  const [search, setSearch] = useState("");

  useEffect(() => {
    let cancelled = false;
    reflexio
      .getUserPlaybooks({ reflexioUrl })
      .then((res) => {
        if (!cancelled) {
          setPlaybooks(res.user_playbooks ?? []);
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

  const projects = useMemo(() => {
    const set = new Set<string>();
    for (const p of playbooks ?? []) set.add(p.agent_version || "default");
    return Array.from(set).sort();
  }, [playbooks]);

  const filtered = useMemo(() => {
    return (playbooks ?? []).filter((p) => {
      if (agentVersion !== "__all__" && (p.agent_version || "default") !== agentVersion)
        return false;
      if (statusFilter !== "__all__" && statusLabel(p) !== statusFilter)
        return false;
      if (search) {
        const s = search.toLowerCase();
        const hay = `${p.content} ${p.trigger ?? ""} ${p.rationale ?? ""}`.toLowerCase();
        if (!hay.includes(s)) return false;
      }
      return true;
    });
  }, [playbooks, agentVersion, statusFilter, search]);

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Playbooks"
        description="Cross-session rules learned from corrections."
        actions={
          <div className="flex items-center gap-2">
            <Select
              value={agentVersion}
              onValueChange={(v) => setAgentVersion(v ?? "__all__")}
            >
              <SelectTrigger size="sm" className="w-40 text-xs">
                <SelectValue placeholder="Project" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">All projects</SelectItem>
                {projects.map((p) => (
                  <SelectItem key={p} value={p}>
                    {p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={statusFilter}
              onValueChange={(v) => setStatusFilter(v ?? "__all__")}
            >
              <SelectTrigger size="sm" className="w-36 text-xs">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">All</SelectItem>
                <SelectItem value="CURRENT">Current</SelectItem>
                <SelectItem value="PENDING">Pending</SelectItem>
                <SelectItem value="ARCHIVED">Archived</SelectItem>
              </SelectContent>
            </Select>
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search"
              className="h-8 w-48 text-xs"
            />
            <DeleteAllButton
              label={`Delete all${playbooks && playbooks.length > 0 ? ` (${playbooks.length})` : ""}`}
              confirmMessage={`Delete ALL ${playbooks?.length ?? 0} playbooks across every project? This cannot be undone.`}
              disabled={!playbooks || playbooks.length === 0}
              onConfirm={async () => {
                await reflexio.deleteAllUserPlaybooks(reflexioUrl);
                setPlaybooks([]);
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

        {playbooks === null && !error ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={BookOpen}
            title="No playbooks match"
            description="Adjust the filters or run /smart-sync to extract rules from recent interactions."
          />
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {filtered.map((p) => (
              <Link
                key={p.user_playbook_id}
                href={`/playbooks/${p.user_playbook_id}`}
                className="block rounded-xl border border-border bg-card p-4 hover:bg-accent/40 transition-colors"
              >
                <header className="flex items-center justify-between gap-2 mb-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <Badge variant="outline" className="h-5 font-mono text-[10px]">
                      {p.agent_version || "default"}
                    </Badge>
                    <Badge
                      variant={
                        statusLabel(p) === "CURRENT"
                          ? "secondary"
                          : statusLabel(p) === "ARCHIVED"
                            ? "outline"
                            : "default"
                      }
                      className="h-5 text-[10px]"
                    >
                      {statusLabel(p)}
                    </Badge>
                  </div>
                  <span className="text-[11px] text-muted-foreground shrink-0">
                    {formatRelative(p.created_at)}
                  </span>
                </header>
                <p className="text-sm leading-relaxed line-clamp-4">{p.content}</p>
                {p.trigger && (
                  <p className="text-xs text-muted-foreground mt-2 line-clamp-2">
                    <span className="font-medium">When:</span> {p.trigger}
                  </p>
                )}
                {p.rationale && (
                  <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                    <span className="font-medium">Why:</span> {p.rationale}
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
