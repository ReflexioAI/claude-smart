"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  BookOpen,
  ChevronRight,
  Layers3,
  Sparkles,
} from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { HostBadge } from "@/components/common/host-badge";
import { EmptyState } from "@/components/common/empty-state";
import { DeleteAllButton } from "@/components/common/delete-all-button";
import { PageTabs } from "@/components/common/page-tabs";
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
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  agentPlaybookStatusLabel,
  statusLabel,
  type AgentPlaybookStatusLabel,
  type StatusLabel,
} from "@/lib/status";
import type {
  AgentPlaybook,
  Host,
  PlaybookApplicationStat,
  SessionSummary,
  UserPlaybook,
} from "@/lib/types";

type SkillKind = "project" | "shared";
type SkillStatus = StatusLabel | AgentPlaybookStatusLabel;
type SkillSort = "newest" | "applied";

const ALL_LIFECYCLE_STATUSES: (string | null)[] = [null, "pending", "archived"];

function hostByRequestId(sessions: SessionSummary[]): Map<string, Host | null> {
  const result = new Map<string, Host | null>();
  for (const session of sessions) {
    for (const requestId of session.request_ids) {
      result.set(requestId, session.host);
    }
  }
  return result;
}

const SHARED_STATUS_META: Record<
  AgentPlaybookStatusLabel,
  { label: string; description: string }
> = {
  PENDING: {
    label: "Auto generated",
    description: "Auto-generated shared skill. It may be updated automatically.",
  },
  APPROVED: {
    label: "Persisted",
    description: "Persisted shared skill. It will not be auto updated.",
  },
  REJECTED: {
    label: "Rejected",
    description: "Rejected shared skill. It will not be used in claude-smart.",
  },
};

interface SkillCard {
  kind: SkillKind;
  id: number;
  createdAt: number;
  content: string;
  trigger: string | null;
  rationale: string | null;
  status: SkillStatus;
  scopeId: string;
  host: Host | null;
}

function projectSkill(
  p: UserPlaybook,
  requestHosts: Map<string, Host | null>,
): SkillCard {
  return {
    kind: "project",
    id: p.user_playbook_id,
    createdAt: p.created_at,
    content: p.content,
    trigger: p.trigger,
    rationale: p.rationale,
    status: statusLabel(p),
    scopeId: p.user_id || "unknown",
    host: requestHosts.get(p.request_id) ?? null,
  };
}

function sharedSkill(p: AgentPlaybook): SkillCard {
  return {
    kind: "shared",
    id: p.agent_playbook_id,
    createdAt: p.created_at,
    content: p.content,
    trigger: p.trigger,
    rationale: p.rationale,
    status: agentPlaybookStatusLabel(p),
    scopeId: p.agent_version || "default",
    host: null,
  };
}

function skillStatKey(skill: SkillCard): string {
  const sourceKind =
    skill.kind === "shared" ? "agent_playbook" : "user_playbook";
  return `playbook:${sourceKind}:${skill.id}`;
}

export default function SkillsPage() {
  const [projectSkills, setProjectSkills] = useState<UserPlaybook[] | null>(null);
  const [sharedSkills, setSharedSkills] = useState<AgentPlaybook[] | null>(null);
  const [appStats, setAppStats] = useState<PlaybookApplicationStat[] | null>(
    null,
  );
  const [requestHosts, setRequestHosts] = useState<Map<string, Host | null>>(
    () => new Map(),
  );
  const [error, setError] = useState<string | null>(null);
  const [attributionUnavailable, setAttributionUnavailable] = useState(false);
  const [activeKind, setActiveKind] = useState<SkillKind>("project");
  const [scope, setScope] = useState<string>("__all__");
  const [statusFilter, setStatusFilter] = useState<string>("CURRENT");
  const [sortBy, setSortBy] = useState<SkillSort>("newest");
  const [search, setSearch] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [projectRes, sharedRes, statsRes, attribution] = await Promise.all([
          reflexio.getUserPlaybooks({
            limit: 500,
            statusFilter: ALL_LIFECYCLE_STATUSES,
          }),
          reflexio.getAgentPlaybooks({
            limit: 500,
            statusFilter: ALL_LIFECYCLE_STATUSES,
          }),
          fetch("/api/rules/applied?daysBack=30&limit=200", {
            cache: "no-store",
          })
            .then((r) => r.json())
            .catch(() => ({
              success: false,
              stats: [] as PlaybookApplicationStat[],
            })),
          fetch("/api/sessions", { cache: "no-store" })
            .then(async (response) => {
              if (!response.ok) throw new Error(`sessions ${response.status}`);
              const data = await response.json();
              return {
                sessions: (data.sessions ?? []) as SessionSummary[],
                failed: false,
              };
            })
            .catch(() => ({
              sessions: [] as SessionSummary[],
              failed: true,
            })),
        ]);
        if (cancelled) return;
        setProjectSkills(projectRes.user_playbooks ?? []);
        setSharedSkills(sharedRes.agent_playbooks ?? []);
        setAppStats(statsRes.stats ?? []);
        setRequestHosts(hostByRequestId(attribution.sessions));
        setAttributionUnavailable(attribution.failed);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const statsByRule = useMemo(() => {
    const map = new Map<string, PlaybookApplicationStat>();
    for (const s of appStats ?? []) {
      map.set(`${s.kind}:${s.source_kind ?? "unknown"}:${s.real_id}`, s);
    }
    return map;
  }, [appStats]);

  const activeSkills = useMemo(() => {
    return activeKind === "project"
      ? (projectSkills ?? []).map((playbook) =>
          projectSkill(playbook, requestHosts),
        )
      : (sharedSkills ?? []).map(sharedSkill);
  }, [activeKind, projectSkills, requestHosts, sharedSkills]);

  const scopes = useMemo(() => {
    const set = new Set<string>();
    for (const p of activeSkills) set.add(p.scopeId);
    return Array.from(set).sort();
  }, [activeSkills]);

  const filtered = useMemo(() => {
    const matches = activeSkills.filter((p) => {
      if (scope !== "__all__" && p.scopeId !== scope)
        return false;
      if (statusFilter !== "__all__" && p.status !== statusFilter) return false;
      if (search) {
        const s = search.toLowerCase();
        const hay = `${p.scopeId} ${p.content} ${p.trigger ?? ""} ${p.rationale ?? ""}`.toLowerCase();
        if (!hay.includes(s)) return false;
      }
      return true;
    });
    return matches.sort((a, b) => {
      if (sortBy === "applied") {
        const aStat = statsByRule.get(skillStatKey(a));
        const bStat = statsByRule.get(skillStatKey(b));
        const appliedDelta =
          (bStat?.applied_count ?? 0) - (aStat?.applied_count ?? 0);
        if (appliedDelta !== 0) return appliedDelta;
        const recencyDelta =
          (bStat?.last_applied_at ?? 0) - (aStat?.last_applied_at ?? 0);
        if (recencyDelta !== 0) return recencyDelta;
      }
      return b.createdAt - a.createdAt;
    });
  }, [activeSkills, scope, search, sortBy, statsByRule, statusFilter]);

  const projectCount = projectSkills?.length ?? 0;
  const sharedCount = sharedSkills?.length ?? 0;
  const visibleActiveCount = filtered.length;
  const activeCount = activeKind === "project" ? projectCount : sharedCount;
  const loading = projectSkills === null || sharedSkills === null;
  const hasNoSharedSkills = activeKind === "shared" && sharedCount === 0;
  const allScopesLabel =
    activeKind === "project" ? "All user scopes" : "All agents";

  const switchKind = (kind: SkillKind) => {
    setActiveKind(kind);
    setScope("__all__");
    setStatusFilter(kind === "project" ? "CURRENT" : "__all__");
  };

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Skills"
        description="Project-specific and shared skills learned from corrections."
        actions={
          <div className="flex max-w-full flex-wrap items-center justify-end gap-2">
            <Select
              value={scope}
              onValueChange={(v) => setScope(v ?? "__all__")}
            >
              <SelectTrigger size="sm" className="w-40 text-xs bg-background/80">
                <SelectValue>
                  {scope === "__all__" ? allScopesLabel : scope}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">{allScopesLabel}</SelectItem>
                {scopes.map((p) => (
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
              <SelectTrigger size="sm" className="w-36 text-xs bg-background/80">
                <SelectValue placeholder="Status">
                  {statusFilterLabel(activeKind, statusFilter)}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">All</SelectItem>
                {activeKind === "project" ? (
                  <>
                    <SelectItem value="CURRENT">Current</SelectItem>
                    <SelectItem value="PENDING">Pending</SelectItem>
                    <SelectItem value="ARCHIVED">Archived</SelectItem>
                  </>
                ) : (
                  <>
                    <SelectItem
                      value="PENDING"
                      title={SHARED_STATUS_META.PENDING.description}
                    >
                      {SHARED_STATUS_META.PENDING.label}
                    </SelectItem>
                    <SelectItem
                      value="APPROVED"
                      title={SHARED_STATUS_META.APPROVED.description}
                    >
                      {SHARED_STATUS_META.APPROVED.label}
                    </SelectItem>
                    <SelectItem
                      value="REJECTED"
                      title={SHARED_STATUS_META.REJECTED.description}
                    >
                      {SHARED_STATUS_META.REJECTED.label}
                    </SelectItem>
                  </>
                )}
              </SelectContent>
            </Select>
            <Select
              value={sortBy}
              onValueChange={(v) => setSortBy((v as SkillSort) ?? "newest")}
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
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search"
              className="h-9 w-48 text-xs bg-background/80"
            />
            <DeleteAllButton
              label={`Delete ${activeKind === "project" ? "project" : "shared"}${activeCount > 0 ? ` (${activeCount})` : ""}`}
              confirmMessage={`Delete ALL ${activeCount} ${activeKind === "project" ? "project-specific skills" : "shared skills"}? This cannot be undone.`}
              disabled={activeCount === 0}
              onConfirm={async () => {
                if (activeKind === "project") {
                  await reflexio.deleteAllUserPlaybooks();
                  setProjectSkills([]);
                } else {
                  await reflexio.deleteAllAgentPlaybooks();
                  setSharedSkills([]);
                }
              }}
            />
          </div>
        }
      />

      <div className="p-6 space-y-4">
        <PageTabs
          activeId={activeKind}
          onSelect={(id) => switchKind(id as SkillKind)}
          items={[
            {
              id: "project",
              label: "Project-specific skills",
              description: "Repo-local rules learned from direct corrections",
              count: activeKind === "project" ? visibleActiveCount : projectCount,
              icon: BookOpen,
            },
            {
              id: "shared",
              label: "Shared skills",
              description: "Rollups available across projects",
              count: activeKind === "shared" ? visibleActiveCount : sharedCount,
              icon: Layers3,
            },
          ]}
        />

        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm">
            {error}. Is reflexio running on the configured backend URL?
          </div>
        )}
        {attributionUnavailable && !error && (
          <div className="mb-4 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-200">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            Skill source details are temporarily unavailable. Skills remain
            available.
          </div>
        )}

        {loading && !error ? (
          <div className="text-sm text-muted-foreground">Loading...</div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={activeKind === "project" ? BookOpen : Layers3}
            title={
              hasNoSharedSkills
                ? "No shared skills yet"
                : `No ${activeKind === "project" ? "project-specific skills" : "shared skills"} match`
            }
            description={
              hasNoSharedSkills
                ? "Shared skills become available after claude-smart has learnings from more than one repo. Keep using Claude with claude-smart enabled across projects, and shared patterns will appear here when they emerge."
                : "Adjust the filters, or keep using Claude with claude-smart enabled. Skills are extracted automatically when patterns emerge."
            }
          />
        ) : (
          <div className="overflow-hidden rounded-lg border border-border bg-card/92 shadow-sm">
            {filtered.map((p) => {
              const stat = statsByRule.get(skillStatKey(p));
              return (
                <Link
                  key={`${p.kind}:${p.id}`}
                  href={`/skills/${p.kind}/${p.id}`}
                  className="group block border-b border-border px-4 py-3.5 transition-colors last:border-b-0 hover:bg-accent/35"
                >
                  <header className="mb-2 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <ApplicationStatBadge stat={stat} />
                      <StatusBadge kind={p.kind} status={p.status} />
                      <Badge variant="secondary" className="h-5 text-[10px]">
                        {p.kind === "project" ? "project-specific" : "shared"}
                      </Badge>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5 pt-0.5">
                      <span className="text-[11px] text-muted-foreground">
                        {formatRelative(p.createdAt)}
                      </span>
                      <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/60 transition-colors group-hover:text-foreground" />
                    </div>
                  </header>
                  <p
                    className={cn(
                      "max-w-5xl text-sm leading-relaxed line-clamp-3",
                      !p.trigger && "text-muted-foreground italic",
                    )}
                  >
                    <span className="mr-2 align-baseline text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                      Trigger
                    </span>
                    {p.trigger || "Always applies"}
                  </p>
                  <p className="mt-2 max-w-5xl text-xs leading-relaxed text-muted-foreground line-clamp-2">
                    <span className="font-medium text-foreground/80">Rule:</span>{" "}
                    {p.content}
                  </p>
                  {p.rationale && (
                    <p className="mt-1 max-w-5xl text-xs leading-relaxed text-muted-foreground line-clamp-2">
                      <span className="font-medium text-foreground/80">Why:</span>{" "}
                      {p.rationale}
                    </p>
                  )}
                  {p.kind === "project" && (
                    <div className="mt-2">
                      <HostBadge host={p.host} display="provenance" />
                    </div>
                  )}
                </Link>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function ApplicationStatBadge({ stat }: { stat: PlaybookApplicationStat | undefined }) {
  if (!stat || stat.applied_count === 0) {
    return (
      <Badge
        variant="outline"
        className="h-5 text-[10px] text-muted-foreground"
        title="No citations recorded yet for this rule. It will count once an assistant reply cites it."
      >
        Never applied
      </Badge>
    );
  }
  const last = formatRelative(stat.last_applied_at);
  return (
    <Badge
      variant="outline"
      className="h-5 gap-1 border-amber-500/45 bg-amber-500/10 text-[10px] text-amber-700 dark:text-amber-300"
      title={`Last applied ${last}`}
    >
      <Sparkles className="h-2.5 w-2.5 text-amber-500" />
      Applied {stat.applied_count}×{stat.last_applied_at ? ` · ${last}` : ""}
    </Badge>
  );
}

function statusFilterLabel(kind: SkillKind, status: string): string {
  if (status === "__all__") return "All";
  if (kind === "shared") {
    const meta = SHARED_STATUS_META[status as AgentPlaybookStatusLabel];
    if (meta) return meta.label;
  }
  if (status === "CURRENT") return "Current";
  if (status === "PENDING") return "Pending";
  if (status === "ARCHIVED") return "Archived";
  return status;
}

function StatusBadge({
  kind,
  status,
}: {
  kind: SkillKind;
  status: SkillStatus;
}) {
  const sharedMeta =
    kind === "shared"
      ? SHARED_STATUS_META[status as AgentPlaybookStatusLabel]
      : null;
  const variant =
    status === "CURRENT" || status === "APPROVED"
      ? "secondary"
      : status === "ARCHIVED" || status === "REJECTED"
        ? "outline"
        : "default";
  return (
    <Badge
      variant={variant}
      className="h-5 text-[10px]"
      title={sharedMeta?.description}
    >
      {sharedMeta?.label ?? status}
    </Badge>
  );
}
