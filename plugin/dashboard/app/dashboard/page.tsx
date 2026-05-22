"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  BookOpen,
  MessageSquare,
  Sparkles,
  Activity,
  ExternalLink,
  Users,
} from "lucide-react";
import { LearningsBadge } from "@/components/common/learnings-badge";
import { PageHeader } from "@/components/common/page-header";
import { StatCard } from "@/components/common/stat-card";
import { EmptyState } from "@/components/common/empty-state";
import { Badge } from "@/components/ui/badge";
import { reflexio } from "@/lib/reflexio-client";
import { formatRelative, truncate, truncateId } from "@/lib/format";
import { agentPlaybookStatusLabel } from "@/lib/status";
import type {
  AgentPlaybook,
  PlaybookApplicationStat,
  SessionSummary,
  UserPlaybook,
  UserProfile,
} from "@/lib/types";

type RecentLearningKind = "project-skill" | "shared-skill" | "preference";

interface RecentLearning {
  id: string;
  kind: RecentLearningKind;
  href: string;
  content: string;
  createdAt: number;
  statKey: string;
}

export default function DashboardPage() {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [projectSkills, setProjectSkills] = useState<UserPlaybook[] | null>(null);
  const [sharedSkills, setSharedSkills] = useState<AgentPlaybook[] | null>(null);
  const [preferences, setPreferences] = useState<UserProfile[] | null>(null);
  const [topApplied, setTopApplied] = useState<PlaybookApplicationStat[] | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setError(null);
      try {
        const [sRes, projectRes, sharedRes, prefRes, statsRes] = await Promise.all([
          fetch("/api/sessions", { cache: "no-store" }).then((r) => r.json()),
          reflexio
            .getUserPlaybooks({})
            .catch(() => ({ user_playbooks: [] as UserPlaybook[] })),
          reflexio
            .getAgentPlaybooks({})
            .catch(() => ({ agent_playbooks: [] as AgentPlaybook[] })),
          reflexio
            .getAllProfiles({ limit: 100 })
            .catch(() => ({ user_profiles: [] as UserProfile[] })),
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
        setSessions(sRes.sessions ?? []);
        setProjectSkills(projectRes.user_playbooks ?? []);
        setSharedSkills(sharedRes.agent_playbooks ?? []);
        setPreferences(prefRes.user_profiles ?? []);
        setTopApplied(statsRes.stats ?? []);
      } catch (e) {
        if (!cancelled)
          setError(e instanceof Error ? e.message : "failed to load");
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  // CURRENT project-specific skills arrive as `status: null` (response_model_exclude_none
  // strips the field). Anything else (e.g. "archived", "pending") is excluded.
  const currentProjectSkills = (projectSkills ?? []).filter((p) => p.status == null);
  const approvedSharedSkills = (sharedSkills ?? []).filter(
    (p) => agentPlaybookStatusLabel(p) === "APPROVED",
  );
 const currentPreferences = (preferences ?? []).filter((p) => p.status == null);
  const statsByRule = useMemo(() => {
    const map = new Map<string, PlaybookApplicationStat>();
    for (const s of topApplied ?? []) {
      map.set(`${s.kind}:${s.source_kind ?? "unknown"}:${s.real_id}`, s);
    }
    return map;
  }, [topApplied]);
  const recentLearnings = useMemo(() => {
    const items: RecentLearning[] = [
      ...currentProjectSkills.map((p) => ({
        id: `project:${p.user_playbook_id}`,
        kind: "project-skill" as const,
        href: `/skills/project/${encodeURIComponent(p.user_playbook_id)}`,
        content: p.content,
        createdAt: p.created_at,
        statKey: `playbook:user_playbook:${p.user_playbook_id}`,
      })),
      ...approvedSharedSkills.map((p) => ({
        id: `shared:${p.agent_playbook_id}`,
        kind: "shared-skill" as const,
        href: `/skills/shared/${encodeURIComponent(p.agent_playbook_id)}`,
        content: p.content,
        createdAt: p.created_at,
        statKey: `playbook:agent_playbook:${p.agent_playbook_id}`,
      })),
      ...currentPreferences.map((p) => ({
        id: `preference:${p.profile_id}`,
        kind: "preference" as const,
        href: `/preferences/project/${encodeURIComponent(p.profile_id)}`,
        content: p.content,
        createdAt: p.last_modified_timestamp,
        statKey: `profile:profile:${p.profile_id}`,
      })),
    ];
    const seen = new Set<string>();
    return items
      .sort((a, b) => b.createdAt - a.createdAt)
      .filter((item) => {
        const key = `${item.kind}:${item.content.trim().toLowerCase()}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, 4);
  }, [approvedSharedSkills, currentPreferences, currentProjectSkills]);
  const learningInteractionTotal = (sessions ?? []).reduce(
    (acc, s) => acc + s.learning_interaction_count,
    0,
  );
  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Dashboard"
        description="Overview of claude-smart learning across sessions and projects."
      />

      <div className="p-6 space-y-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <StatCard
            label="Sessions recorded"
            value={sessions?.length ?? "—"}
            hint="JSONL buffers on disk"
            icon={Activity}
          />
          <StatCard
            label="Project-specific skills"
            value={currentProjectSkills.length || "—"}
            hint="current project-specific rules"
            icon={BookOpen}
          />
          <StatCard
            label="Shared skills"
            value={approvedSharedSkills.length || "—"}
            hint="approved shared rules"
            icon={BookOpen}
          />
          <StatCard
            label="Interactions with learnings applied"
            value={learningInteractionTotal}
            hint="turns where a skill or preference was cited"
            icon={Sparkles}
          />
        </div>

        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm">
            {error}
          </div>
        )}

        <div className="grid min-w-0 gap-6 lg:grid-cols-2">
          <section className="min-w-0">
            <div className="flex items-center justify-between mb-3">
              <div>
                <h2 className="text-sm font-semibold">Recent sessions</h2>
                <p className="text-xs text-muted-foreground">
                  Latest local buffers and cited learnings.
                </p>
              </div>
              <Link
                href="/sessions"
                className="text-xs text-primary hover:text-foreground inline-flex items-center gap-1"
              >
                View all <ExternalLink className="h-3 w-3" />
              </Link>
            </div>
            {sessions && sessions.length > 0 ? (
              <div className="rounded-lg border border-border divide-y divide-border bg-card/92 shadow-sm overflow-hidden">
                {sessions.slice(0, 5).map((s) => (
                  <Link
                    key={s.session_id}
                    href={`/sessions/${s.session_id}`}
                    className="flex flex-col gap-2 px-4 py-3 transition-colors hover:bg-accent/45 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0 flex w-full items-center gap-3">
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
                        <MessageSquare className="h-4 w-4" />
                      </span>
                      <code className="font-mono text-xs truncate">
                        {truncateId(s.session_id, 10, 6)}
                      </code>
                      <LearningsBadge count={s.learning_interaction_count} />
                    </div>
                    <div className="flex w-full items-center justify-end gap-4 text-xs text-muted-foreground sm:w-auto sm:shrink-0">
                      <span>{s.turn_count} turns</span>
                      <span>{formatRelative(s.last_activity)}</span>
                    </div>
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState
                icon={MessageSquare}
                title="No sessions yet"
                description="Run Claude Code with claude-smart enabled — sessions will appear here."
              />
            )}
          </section>

          <section className="min-w-0">
            <div className="flex items-center justify-between mb-3">
              <div>
                <h2 className="text-sm font-semibold">Recent learnings</h2>
                <p className="text-xs text-muted-foreground">
                  New skills and preferences extracted from recent patterns.
                </p>
              </div>
              <div className="flex items-center gap-3">
                <Link
                  href="/skills"
                  className="text-xs text-primary hover:text-foreground inline-flex items-center gap-1"
                >
                  Skills <ExternalLink className="h-3 w-3" />
                </Link>
                <Link
                  href="/preferences"
                  className="text-xs text-primary hover:text-foreground inline-flex items-center gap-1"
                >
                  Preferences <ExternalLink className="h-3 w-3" />
                </Link>
              </div>
            </div>
            {recentLearnings.length > 0 ? (
              <div className="rounded-lg border border-border divide-y divide-border bg-card/92 shadow-sm overflow-hidden">
                {recentLearnings.map((item) => {
                  const stat = statsByRule.get(item.statKey);
                  return (
                    <Link
                      key={item.id}
                      href={item.href}
                      className="flex min-w-0 items-center justify-between gap-3 px-4 py-3 transition-colors hover:bg-accent/45"
                    >
                      <div className="min-w-0 flex flex-1 items-center gap-3">
                        {learningIcon(item.kind)}
                        <div className="min-w-0 flex-1">
                          <p className="text-sm truncate">{item.content}</p>
                          <div className="mt-1 flex flex-wrap items-center gap-2">
                            <Badge variant="outline" className="h-5 text-[10px]">
                              {learningKindLabel(item.kind)}
                            </Badge>
                            <span className="text-[11px] text-muted-foreground">
                              {learningScope(item.kind)}
                            </span>
                            <LearningApplicationStatBadge stat={stat} />
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-3 text-xs text-muted-foreground shrink-0">
                        <span>{formatRelative(item.createdAt)}</span>
                      </div>
                    </Link>
                  );
                })}
              </div>
            ) : (
              <EmptyState
                icon={BookOpen}
                title="No learnings yet"
                description="Keep using Claude with claude-smart enabled. Skills and preferences are extracted automatically when patterns emerge."
              />
            )}
          </section>

          <section className="min-w-0 lg:col-span-2">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-semibold">
                  Most used claude-smart learnings
                </h2>
                <span className="text-[11px] text-muted-foreground">
                  last 30 days
                </span>
              </div>
              <Link
                href="/sessions"
                className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
              >
                Review sessions <ExternalLink className="h-3 w-3" />
              </Link>
            </div>
            {topApplied === null ? (
              <div className="text-sm text-muted-foreground">Loading...</div>
            ) : topApplied.length > 0 ? (
              <div className="rounded-lg border border-border divide-y divide-border bg-card/92 shadow-sm">
                {topApplied.slice(0, 5).map((s) => {
                  const href = s.href ?? null;
                  const label = appliedRuleLabel(s);
                  const rowBody = (
                    <>
                      <div className="min-w-0 flex flex-1 items-center gap-3">
                        <Sparkles className="h-4 w-4 text-muted-foreground shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm truncate">
                            {s.title || (
                              <span className="text-muted-foreground italic">
                                (rule removed)
                              </span>
                            )}
                          </p>
                          <p className="text-[11px] text-muted-foreground">
                            {label} · {truncate(s.real_id, 12)}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center gap-3 text-xs text-muted-foreground shrink-0">
                        <Badge variant="secondary" className="text-[10px]">
                          Used {s.applied_count}×
                        </Badge>
                        <span>{formatRelative(s.last_applied_at)}</span>
                      </div>
                    </>
                  );
                  return href ? (
                    <Link
                      key={`${s.kind}:${s.source_kind ?? "unknown"}:${s.real_id}`}
                      href={href}
                      className="flex min-w-0 items-center justify-between gap-3 px-4 py-3 hover:bg-accent/40 transition-colors"
                    >
                      {rowBody}
                    </Link>
                  ) : (
                    <div
                      key={`${s.kind}:${s.source_kind ?? "unknown"}:${s.real_id}`}
                      className="flex min-w-0 items-center justify-between gap-3 px-4 py-3"
                    >
                      {rowBody}
                    </div>
                  );
                })}
              </div>
            ) : (
              <EmptyState
                icon={Sparkles}
                title="No applied learnings yet"
                description="When a claude-smart learning is cited in a local assistant response, it will appear here with usage and recency."
              />
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

function appliedRuleLabel(stat: PlaybookApplicationStat): string {
  if (stat.kind === "profile") return "preference";
  return stat.source_kind === "agent_playbook" ? "shared skill" : "project skill";
}

function learningScope(kind: RecentLearningKind): string {
  if (kind === "shared-skill") return "shared";
  return "project";
}

function learningIcon(kind: RecentLearningKind) {
  const Icon = kind === "preference" ? Users : BookOpen;
  return <Icon className="h-4 w-4 text-muted-foreground shrink-0" />;
}

function learningKindLabel(kind: RecentLearningKind): string {
  return kind === "preference" ? "preference" : "skill";
}

function LearningApplicationStatBadge({
  stat,
}: {
  stat: PlaybookApplicationStat | undefined;
}) {
  if (!stat || stat.applied_count === 0) {
    return (
      <Badge
        variant="outline"
        className="h-5 text-[10px] text-muted-foreground"
      >
        Never applied
      </Badge>
    );
  }
  const last = formatRelative(stat.last_applied_at);
  return (
    <Badge variant="secondary" className="h-5 text-[10px]">
      Applied {stat.applied_count}×{stat.last_applied_at ? ` · ${last}` : ""}
    </Badge>
  );
}
