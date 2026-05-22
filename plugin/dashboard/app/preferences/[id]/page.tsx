"use client";

import { use, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Save,
  AlertTriangle,
  Pencil,
  X,
  Copy,
  Check,
  Hash,
  Clock,
  CalendarClock,
  FileText,
  Sparkles,
  Tags,
  Braces,
  FolderGit2,
} from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { EmptyState } from "@/components/common/empty-state";
import { DeleteLearningDangerZone } from "@/components/common/delete-learning-danger-zone";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { reflexio } from "@/lib/reflexio-client";
import { formatTimestamp, truncateId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { statusLabel as status } from "@/lib/status";
import type { UserProfile } from "@/lib/types";

export default function PreferenceDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: rawId } = use(params);
  const id = decodeURIComponent(rawId);
  const router = useRouter();

  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState("");

  useEffect(() => {
    let cancelled = false;
    reflexio
      .getAllProfiles({ limit: 500 })
      .then((res) => {
        if (cancelled) return;
        const found = (res.user_profiles ?? []).find(
          (p) => p.profile_id === id,
        );
        if (!found) {
          setNotFound(true);
          return;
        }
        setProfile(found);
        setContent(found.content);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const dirty = useMemo(
    () => !!profile && profile.content !== content,
    [profile, content],
  );

  const save = async () => {
    if (!profile || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      await reflexio.updateUserProfile(
        {
          user_id: profile.user_id,
          profile_id: profile.profile_id,
          content,
        },
      );
      setProfile({ ...profile, content });
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!profile) return;
    setDeleting(true);
    try {
      await reflexio.deleteUserProfile(
        { user_id: profile.user_id, profile_id: profile.profile_id },
      );
      router.push("/preferences");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  };

  const cancelEdit = () => {
    if (profile) setContent(profile.content);
    setEditing(false);
  };

  if (notFound) {
    return (
      <div className="flex-1 overflow-auto">
        <PageHeader title="Preference not found" />
        <div className="p-6 max-w-2xl mx-auto">
          <EmptyState
            icon={AlertTriangle}
            title="Preference not found"
            description="It may have been deleted, archived, or moved outside the retrieval window."
            action={
              <Link href="/preferences">
                <Button variant="outline" size="sm">
                  <ArrowLeft className="h-3.5 w-3.5" />
                  Back to preferences
                </Button>
              </Link>
            }
          />
        </div>
      </div>
    );
  }

  const customEntries = profile?.custom_features
    ? Object.entries(profile.custom_features).filter(
        ([, v]) => v !== null && v !== undefined && v !== "",
      )
    : [];

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Preference"
        description="Project-scoped preference extracted by claude-smart."
        actions={
          <div className="flex items-center gap-2">
            <Link href="/preferences">
              <Button variant="outline" size="sm">
                <ArrowLeft className="h-3.5 w-3.5" />
                Back
              </Button>
            </Link>
            {!editing ? (
              <Button
                size="sm"
                onClick={() => setEditing(true)}
                disabled={!profile}
              >
                <Pencil className="h-3.5 w-3.5" />
                Edit
              </Button>
            ) : (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={cancelEdit}
                  disabled={saving}
                >
                  <X className="h-3.5 w-3.5" />
                  Cancel
                </Button>
                <Button size="sm" onClick={save} disabled={saving || !dirty}>
                  <Save className="h-3.5 w-3.5" />
                  {saving ? "Saving…" : "Save"}
                </Button>
              </>
            )}
          </div>
        }
      />

      <div className="p-6">
        <div className="mx-auto max-w-5xl grid gap-6 lg:grid-cols-[1fr_280px]">
          <div className="space-y-6 min-w-0">
            {error && (
              <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm flex items-start gap-2">
                <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {profile && (
              <div className="flex items-center gap-2 flex-wrap">
                <StatusBadge status={status(profile)} />
                <Badge variant="outline" className="font-mono gap-1.5">
                  <FolderGit2 className="h-3 w-3" />
                  {truncateId(profile.user_id, 32, 8)}
                </Badge>
                {profile.source && (
                  <Badge variant="secondary" className="font-mono text-[10px]">
                    {profile.source}
                  </Badge>
                )}
                {dirty && (
                  <Badge variant="destructive" className="gap-1.5">
                    unsaved changes
                  </Badge>
                )}
              </div>
            )}

            <Section
              icon={Sparkles}
              title="Preference"
              hint="Project-scoped preference. Reinjected into future sessions in this project until its configured TTL expires."
            >
              {editing ? (
                <textarea
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  rows={6}
                  placeholder="e.g. Project bans pytest-asyncio; uses anyio with trio backend for async tests."
                  className="w-full rounded-lg border border-input bg-transparent px-4 py-3 text-sm leading-relaxed font-sans resize-y outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 placeholder:text-muted-foreground"
                />
              ) : (
                <Prose text={profile?.content ?? ""} />
              )}
            </Section>

            {profile?.extractor_names && profile.extractor_names.length > 0 && (
              <Section
                icon={Tags}
                title="Extractors"
                hint="Which reflexio extractor generated this preference."
              >
                <div className="flex flex-wrap gap-1.5">
                  {profile.extractor_names.map((name) => (
                    <Badge
                      key={name}
                      variant="outline"
                      className="font-mono text-[10px]"
                    >
                      {name}
                    </Badge>
                  ))}
                </div>
              </Section>
            )}

            {customEntries.length > 0 && (
              <Section
                icon={Braces}
                title="Custom features"
                hint="Structured metadata attached to this preference."
              >
                <div className="rounded-lg border border-border bg-card overflow-hidden">
                  <dl className="divide-y divide-border">
                    {customEntries.map(([k, v]) => (
                      <div
                        key={k}
                        className="flex items-start justify-between gap-4 px-4 py-2.5"
                      >
                        <dt className="text-xs font-medium text-muted-foreground font-mono shrink-0">
                          {k}
                        </dt>
                        <dd className="text-xs min-w-0 break-words text-right">
                          {typeof v === "string"
                            ? v
                            : JSON.stringify(v, null, 0)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              </Section>
            )}

            {!editing && profile && (
              <>
                <Separator />
                <DangerZone
                  onDelete={remove}
                  deleting={deleting}
                  disabled={saving}
                />
              </>
            )}
          </div>

          {profile && (
            <aside className="space-y-3 lg:sticky lg:top-6 lg:self-start">
              <div className="rounded-lg border border-border bg-card p-4">
                <h3 className="text-xs font-semibold uppercase text-muted-foreground mb-3">
                  Metadata
                </h3>
                <dl className="space-y-2.5 text-sm">
                  <Meta
                    icon={Clock}
                    label="Modified"
                    value={formatTimestamp(profile.last_modified_timestamp)}
                  />
                  {profile.expiration_timestamp &&
                    profile.expiration_timestamp > 0 && (
                      <Meta
                        icon={CalendarClock}
                        label="Expires"
                        value={formatTimestamp(profile.expiration_timestamp)}
                      />
                    )}
                  {profile.profile_time_to_live && (
                    <Meta
                      label="TTL"
                      value={profile.profile_time_to_live}
                      mono
                    />
                  )}
                  <CopyMeta
                    icon={Hash}
                    label="ID"
                    value={profile.profile_id}
                    display={truncateId(profile.profile_id, 8, 4)}
                  />
                  <CopyMeta
                    icon={FolderGit2}
                    label="Project"
                    value={profile.user_id}
                    display={truncateId(profile.user_id, 32, 8)}
                  />
                  {profile.generated_from_request_id && (
                    <CopyMeta
                      icon={FileText}
                      label="Request"
                      value={profile.generated_from_request_id}
                      display={truncateId(profile.generated_from_request_id, 8, 4)}
                    />
                  )}
                </dl>
              </div>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({
  icon: Icon,
  title,
  hint,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 flex-wrap">
        <Label className="text-sm font-semibold flex items-center gap-1.5">
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
          {title}
        </Label>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

function Prose({ text }: { text: string }) {
  if (!text) {
    return <p className="text-sm text-muted-foreground italic">—</p>;
  }
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
        {text}
      </p>
    </div>
  );
}

function StatusBadge({
  status,
}: {
  status: "CURRENT" | "ARCHIVED" | "PENDING";
}) {
  const variant =
    status === "CURRENT"
      ? "secondary"
      : status === "ARCHIVED"
        ? "outline"
        : "default";
  return (
    <Badge variant={variant} className="gap-1.5">
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          status === "CURRENT" && "bg-emerald-500",
          status === "PENDING" && "bg-amber-500",
          status === "ARCHIVED" && "bg-muted-foreground",
        )}
      />
      {status}
    </Badge>
  );
}

function Meta({
  icon: Icon,
  label,
  value,
  mono,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-xs text-muted-foreground shrink-0 flex items-center gap-1.5">
        {Icon && <Icon className="h-3 w-3" />}
        {label}
      </dt>
      <dd
        className={cn(
          "text-xs text-right min-w-0 break-words",
          mono && "font-mono",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function CopyMeta({
  icon: Icon,
  label,
  value,
  display,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  display: string;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // ignore
    }
  };
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-xs text-muted-foreground shrink-0 flex items-center gap-1.5">
        {Icon && <Icon className="h-3 w-3" />}
        {label}
      </dt>
      <dd className="text-xs min-w-0 flex items-center gap-1.5">
        <code className="font-mono">{display}</code>
        <button
          onClick={copy}
          className="text-muted-foreground hover:text-foreground transition-colors"
          title="Copy full id"
        >
          {copied ? (
            <Check className="h-3 w-3 text-emerald-500" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
        </button>
      </dd>
    </div>
  );
}

function DangerZone({
  onDelete,
  deleting,
  disabled,
}: {
  onDelete: () => void;
  deleting: boolean;
  disabled: boolean;
}) {
  return (
    <DeleteLearningDangerZone
      learningName="this preference"
      description="Delete this claude-smart preference permanently."
      consequences={[
        "This preference will stop being applied in future claude-smart responses.",
        "Historical session records may still show prior uses.",
        "Future interactions may learn a similar preference again, but this record cannot be restored.",
      ]}
      onDelete={onDelete}
      deleting={deleting}
      disabled={disabled}
    />
  );
}
