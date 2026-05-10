"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  BookOpen,
  MessageSquare,
  Sparkles,
  Activity,
  ExternalLink,
} from "lucide-react";
import { LearningsBadge } from "@/components/common/learnings-badge";
import { PageHeader } from "@/components/common/page-header";
import { StatCard } from "@/components/common/stat-card";
import { EmptyState } from "@/components/common/empty-state";
import { Badge } from "@/components/ui/badge";
import { reflexio } from "@/lib/reflexio-client";
import { useSettings } from "@/hooks/use-settings";
import { formatRelative, truncateId } from "@/lib/format";
import { agentPlaybookStatusLabel } from "@/lib/status";
import type { AgentPlaybook, SessionSummary, UserPlaybook } from "@/lib/types";

export default function DashboardPage() {
  const { reflexioUrl } = useSettings();
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [projectSkills, setProjectSkills] = useState<UserPlaybook[] | null>(null);
  const [sharedSkills, setSharedSkills] = useState<AgentPlaybook[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setError(null);
      try {
        const [sRes, projectRes, sharedRes] = await Promise.all([
          fetch("/api/sessions", { cache: "no-store" }).then((r) => r.json()),
          reflexio
            .getUserPlaybooks({ reflexioUrl })
            .catch(() => ({ user_playbooks: [] as UserPlaybook[] })),
          reflexio
            .getAgentPlaybooks({ reflexioUrl })
            .catch(() => ({ agent_playbooks: [] as AgentPlaybook[] })),
        ]);
        if (cancelled) return;
        setSessions(sRes.sessions ?? []);
        setProjectSkills(projectRes.user_playbooks ?? []);
        setSharedSkills(sharedRes.agent_playbooks ?? []);
      } catch (e) {
        if (!cancelled)
          setError(e instanceof Error ? e.message : "failed to load");
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [reflexioUrl]);

  // CURRENT project-specific skills arrive as `status: null` (response_model_exclude_none
  // strips the field). Anything else (e.g. "archived", "pending") is excluded.
  const currentProjectSkills = (projectSkills ?? []).filter((p) => p.status == null);
  const approvedSharedSkills = (sharedSkills ?? []).filter(
    (p) => agentPlaybookStatusLabel(p) === "APPROVED",
  );
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

        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold">Recent sessions</h2>
            <Link
              href="/sessions"
              className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
            >
              View all <ExternalLink className="h-3 w-3" />
            </Link>
          </div>
          {sessions && sessions.length > 0 ? (
            <div className="rounded-xl border border-border divide-y divide-border bg-card">
              {sessions.slice(0, 5).map((s) => (
                <Link
                  key={s.session_id}
                  href={`/sessions/${s.session_id}`}
                  className="flex items-center justify-between px-4 py-3 hover:bg-accent/40 transition-colors"
                >
                  <div className="min-w-0 flex items-center gap-3">
                    <MessageSquare className="h-4 w-4 text-muted-foreground shrink-0" />
                    <code className="font-mono text-xs truncate">
                      {truncateId(s.session_id, 10, 6)}
                    </code>
                    <LearningsBadge count={s.learning_interaction_count} />
                  </div>
                  <div className="flex items-center gap-4 text-xs text-muted-foreground shrink-0">
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

        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold">Recent skills</h2>
            <Link
              href="/skills"
              className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
            >
              View all <ExternalLink className="h-3 w-3" />
            </Link>
          </div>
          {currentProjectSkills.length > 0 || approvedSharedSkills.length > 0 ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {currentProjectSkills.slice(0, 2).map((p) => (
                <Link
                  key={`project:${p.user_playbook_id}`}
                  href={`/skills/project/${p.user_playbook_id}`}
                  className="block rounded-xl border border-border bg-card p-4 hover:bg-accent/40 transition-colors"
                >
                  <div className="flex items-center justify-between gap-2 mb-2">
                    <Badge variant="outline" className="font-mono text-[10px]">
                      {p.agent_version || "default"}
                    </Badge>
                    <Badge variant="secondary" className="text-[10px]">
                      project-specific
                    </Badge>
                    <span className="text-[11px] text-muted-foreground">
                      {formatRelative(p.created_at)}
                    </span>
                  </div>
                  <p className="text-sm line-clamp-3">{p.content}</p>
                  {p.trigger && (
                    <p className="text-xs text-muted-foreground mt-2 line-clamp-1">
                      <span className="font-medium">trigger:</span> {p.trigger}
                    </p>
                  )}
                </Link>
              ))}
              {approvedSharedSkills.slice(0, 2).map((p) => (
                <Link
                  key={`shared:${p.agent_playbook_id}`}
                  href={`/skills/shared/${p.agent_playbook_id}`}
                  className="block rounded-xl border border-border bg-card p-4 hover:bg-accent/40 transition-colors"
                >
                  <div className="flex items-center justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="font-mono text-[10px]">
                        {p.agent_version || "default"}
                      </Badge>
                      <Badge variant="secondary" className="text-[10px]">
                        shared
                      </Badge>
                    </div>
                    <span className="text-[11px] text-muted-foreground">
                      {formatRelative(p.created_at)}
                    </span>
                  </div>
                  <p className="text-sm line-clamp-3">{p.content}</p>
                  {p.trigger && (
                    <p className="text-xs text-muted-foreground mt-2 line-clamp-1">
                      <span className="font-medium">trigger:</span> {p.trigger}
                    </p>
                  )}
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={BookOpen}
              title="No skills yet"
              description="Keep using Claude with claude-smart enabled. Skills are extracted automatically when patterns emerge."
            />
          )}
        </section>
      </div>
    </div>
  );
}
