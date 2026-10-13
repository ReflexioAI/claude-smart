"use client";

import { useEffect, useState } from "react";
import Link from "@/components/common/source-link";
import { Activity, BookOpen, Users, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { StatCard } from "@/components/common/stat-card";
import { reflexio } from "@/lib/reflexio-client";
import { useSettings } from "@/hooks/use-settings";
import type { SessionSummary, UserPlaybook, UserProfile } from "@/lib/types";

type Summary = Awaited<
  ReturnType<typeof reflexio.getDashboardStats>
>["stats"]["current_period"];
export default function DashboardPage() {
  const { source, sources } = useSettings();
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [deviceError, setDeviceError] = useState<string | null>(null);
  const [recent, setRecent] = useState<{
    skills: UserPlaybook[];
    profiles: UserProfile[];
  } | null>(null);
  const [recentError, setRecentError] = useState<string | null>(null);
  const [model, setModel] = useState("Loading…");
  useEffect(() => {
    let cancelled = false;
    reflexio
      .getDashboardStats()
      .then((data) => {
        if (!data.stats?.current_period)
          throw new Error("Backend did not report dashboard totals");
        if (!cancelled) setSummary(data.stats.current_period);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e.message || e));
      });
    Promise.all([
      reflexio.getUserPlaybooks({ limit: 4, statusFilter: [null] }),
      reflexio.getAllProfiles({ limit: 4 }),
    ])
      .then(([skills, profiles]) => {
        if (!cancelled)
          setRecent({
            skills: skills.user_playbooks,
            profiles: profiles.user_profiles,
          });
      })
      .catch((e) => {
        if (!cancelled) setRecentError(String(e.message || e));
      });
    reflexio
      .getServerConfiguration()
      .then((config) => {
        if (!cancelled)
          setModel(
            (source === "local" && sources?.local.learningModel
              ? `${sources.local.learningModel} (configured local learning model)`
              : config.llm_config?.generation_model_name) ||
              "Service default (model not reported)",
          );
      })
      .catch(() => {
        if (!cancelled) setModel("Model not reported by this backend");
      });
    fetch("/api/sessions", { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Unable to read device sessions");
        return response.json();
      })
      .then((data) => {
        if (!cancelled) setSessions(data.sessions);
      })
      .catch((e) => {
        if (!cancelled) setDeviceError(String(e.message || e));
      });
    return () => {
      cancelled = true;
    };
  }, [source, sources]);
  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Dashboard"
        description={`${source === "managed" ? "Managed" : "Local"} Reflexio · last 30 days`}
      />
      <div className="p-6 space-y-6">
        {error && (
          <div
            role="alert"
            className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-sm"
          >
            Selected backend unavailable: {error}. Check its configuration or
            select another data source.
          </div>
        )}
        {!summary && !error && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading backend totals…
          </p>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <StatCard
            label="Interactions (30d)"
            value={summary?.total_interactions ?? "—"}
            hint="recorded by the selected backend"
            icon={Activity}
          />
          <StatCard
            label="User profiles (30d)"
            value={summary?.total_profiles ?? "—"}
            hint="current profiles created in this period"
            icon={Users}
          />
          <StatCard
            label="Playbooks (30d)"
            value={summary?.total_playbooks ?? "—"}
            hint="playbooks created in this period"
            icon={BookOpen}
          />
          <StatCard
            label="Agent success rate (30d)"
            value={summary ? `${summary.success_rate.toFixed(1)}%` : "—"}
            hint="evaluations on the selected backend"
            icon={Sparkles}
          />
        </div>
        <p className="text-sm text-muted-foreground">Learning model: {model}</p>
        <section className="border-t border-border pt-5 space-y-3">
          <h2 className="font-semibold">Recent learnings · selected backend</h2>
          {recentError && (
            <p role="alert" className="text-sm text-destructive">
              Unable to load recent learnings: {recentError}
            </p>
          )}
          {!recent && !recentError && (
            <p className="text-sm text-muted-foreground">
              Loading recent learnings…
            </p>
          )}
          {recent && (
            <div className="divide-y divide-border">
              {recent.skills.map((skill) => (
                <Link
                  key={skill.user_playbook_id}
                  className="block py-3 text-sm hover:underline"
                  href={`/skills/project/${skill.user_playbook_id}`}
                >
                  <span className="line-clamp-2 break-words">
                    {skill.content}
                  </span>
                </Link>
              ))}
              {recent.profiles.map((profile) => (
                <Link
                  key={profile.profile_id}
                  className="block py-3 text-sm hover:underline"
                  href={`/preferences/${encodeURIComponent(profile.profile_id)}`}
                >
                  <span className="line-clamp-2 break-words">
                    {profile.content}
                  </span>
                </Link>
              ))}
              {!recent.skills.length && !recent.profiles.length && (
                <p className="text-sm text-muted-foreground">
                  No recent learnings found.
                </p>
              )}
            </div>
          )}
          <div className="flex flex-wrap gap-4 text-sm">
            <Link href="/skills" className="text-primary">
              Browse skills
            </Link>
            <Link href="/preferences" className="text-primary">
              Browse preferences
            </Link>
            <Link href="/sessions" className="text-primary">
              Browse backend sessions
            </Link>
          </div>
        </section>
        <section className="border-t border-border pt-5 space-y-3">
          <h2 className="font-semibold">This device</h2>
          <p className="text-sm text-muted-foreground">
            Session files and hook activity on this machine, independent of the
            selected Reflexio backend.
          </p>
          {deviceError && (
            <p role="alert" className="text-sm text-destructive">
              {deviceError}
            </p>
          )}
          {sessions && (
            <p className="text-sm">
              {sessions.length} session buffers ·{" "}
              {sessions.reduce(
                (total, session) => total + session.learning_interaction_count,
                0,
              )}{" "}
              interactions with cited learnings
            </p>
          )}
          <Link href="/device/sessions" className="text-sm text-primary">
            View device activity
          </Link>
        </section>
      </div>
    </div>
  );
}
