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
  BookMarked,
  Hash,
  FolderGit2,
  Clock,
  FileText,
} from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { EmptyState } from "@/components/common/empty-state";
import { DeleteLearningDangerZone } from "@/components/common/delete-learning-danger-zone";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { reflexio } from "@/lib/reflexio-client";
import { useSettings } from "@/hooks/use-settings";
import { formatTimestamp, truncateId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { statusLabel } from "@/lib/status";
import type { UserPlaybook } from "@/lib/types";

type FormState = { content: string; trigger: string; rationale: string };

function toForm(p: UserPlaybook): FormState {
  return {
    content: p.content,
    trigger: p.trigger ?? "",
    rationale: p.rationale ?? "",
  };
}

function displayName(name: string | null | undefined): string | null {
  if (!name) return null;
  if (name === "default_playbook_extractor") return "project-specific skill";
  return name;
}

export default function ProjectSkillDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const { reflexioUrl } = useSettings();

  const [playbook, setPlaybook] = useState<UserPlaybook | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>({
    content: "",
    trigger: "",
    rationale: "",
  });

  useEffect(() => {
    let cancelled = false;
    reflexio
      .getUserPlaybooks({ reflexioUrl })
      .then((res) => {
        if (cancelled) return;
        const found = (res.user_playbooks ?? []).find(
          (p) => String(p.user_playbook_id) === id,
        );
        if (!found) {
          setNotFound(true);
          return;
        }
        setPlaybook(found);
        setForm(toForm(found));
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [id, reflexioUrl]);

  const dirty = useMemo(() => {
    if (!playbook) return false;
    const orig = toForm(playbook);
    return (
      orig.content !== form.content ||
      orig.trigger !== form.trigger ||
      orig.rationale !== form.rationale
    );
  }, [playbook, form]);

  const save = async () => {
    if (!playbook || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      await reflexio.updateUserPlaybook(
        {
          user_playbook_id: playbook.user_playbook_id,
          content: form.content,
          trigger: form.trigger || null,
          rationale: form.rationale || null,
        },
        reflexioUrl,
      );
      setPlaybook({
        ...playbook,
        content: form.content,
        trigger: form.trigger || null,
        rationale: form.rationale || null,
      });
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!playbook) return;
    setDeleting(true);
    try {
      await reflexio.deleteUserPlaybook(playbook.user_playbook_id, reflexioUrl);
      router.push("/skills");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  };

  const cancelEdit = () => {
    if (playbook) setForm(toForm(playbook));
    setEditing(false);
  };

  if (notFound) {
    return (
      <div className="flex-1 overflow-auto">
        <PageHeader title="Project-specific skill not found" />
        <div className="p-6 max-w-2xl mx-auto">
          <EmptyState
            icon={AlertTriangle}
            title="Project-specific skill not found"
            description="It may have been deleted, archived, or moved outside the first 100 results."
            action={
              <Link href="/skills">
                <Button variant="outline" size="sm">
                  <ArrowLeft className="h-3.5 w-3.5" />
                  Back to skills
                </Button>
              </Link>
            }
          />
        </div>
      </div>
    );
  }

  const status = playbook ? statusLabel(playbook) : null;

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title={`Project-specific skill #${playbook?.user_playbook_id ?? id}`}
        description="Project-specific skill learned by claude-smart."
        actions={
          <div className="flex items-center gap-2">
            <Link href="/skills">
              <Button variant="outline" size="sm">
                <ArrowLeft className="h-3.5 w-3.5" />
                Back
              </Button>
            </Link>
            {!editing ? (
              <Button
                size="sm"
                onClick={() => setEditing(true)}
                disabled={!playbook}
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
                <Button
                  size="sm"
                  onClick={save}
                  disabled={saving || !dirty}
                >
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

            {playbook && (
              <div className="flex items-center gap-2 flex-wrap">
                <Badge variant="outline" className="gap-1.5">
                  <FolderGit2 className="h-3 w-3" />
                  {playbook.agent_version || "default"}
                </Badge>
                <StatusBadge status={status!} />
                {displayName(playbook.playbook_name) && (
                  <Badge variant="secondary" className="font-mono text-[10px]">
                    {displayName(playbook.playbook_name)}
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
              icon={AlertTriangle}
              title="Trigger"
              hint="When this rule should apply. Leave empty if it always applies."
            >
              {editing ? (
                <AutoTextarea
                  value={form.trigger}
                  onChange={(v) => setForm((f) => ({ ...f, trigger: v }))}
                  rows={2}
                  placeholder="e.g. When writing or running async Python tests."
                />
              ) : (
                <Prose text={playbook?.trigger ?? ""} muted={!playbook?.trigger} />
              )}
            </Section>

            <Section
              icon={BookMarked}
              title="Rule"
              hint="What Claude should do. Injected when relevant in future sessions."
            >
              {editing ? (
                <AutoTextarea
                  value={form.content}
                  onChange={(v) => setForm((f) => ({ ...f, content: v }))}
                  rows={6}
                  placeholder="e.g. Use anyio with trio backend — never pytest-asyncio."
                />
              ) : (
                <Prose text={playbook?.content ?? ""} />
              )}
            </Section>

            <Section
              icon={FileText}
              title="Rationale"
              hint="Why — the reason, constraint, or past incident behind this rule."
            >
              {editing ? (
                <AutoTextarea
                  value={form.rationale}
                  onChange={(v) => setForm((f) => ({ ...f, rationale: v }))}
                  rows={3}
                  placeholder="e.g. pytest-asyncio deadlocked CI on project X — trio is the project standard."
                />
              ) : (
                <Prose
                  text={playbook?.rationale ?? ""}
                  muted={!playbook?.rationale}
                />
              )}
            </Section>

            {!editing && playbook && (
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

          {playbook && (
            <aside className="space-y-3 lg:sticky lg:top-6 lg:self-start">
              <div className="rounded-lg border border-border bg-card p-4">
                <h3 className="text-xs font-semibold uppercase text-muted-foreground mb-3">
                  Metadata
                </h3>
                <dl className="space-y-2.5 text-sm">
                  <Meta
                    icon={Hash}
                    label="ID"
                    value={String(playbook.user_playbook_id)}
                    mono
                  />
                  <Meta
                    icon={Clock}
                    label="Created"
                    value={formatTimestamp(playbook.created_at)}
                  />
                  <Meta
                    label="Project"
                    value={playbook.agent_version || "default"}
                    mono
                  />
                  {playbook.user_id && (
                    <CopyMeta
                      label="Project"
                      value={playbook.user_id}
                      display={truncateId(playbook.user_id, 32, 8)}
                    />
                  )}
                  {playbook.request_id && (
                    <CopyMeta
                      label="Request"
                      value={playbook.request_id}
                      display={truncateId(playbook.request_id, 8, 4)}
                    />
                  )}
                  {playbook.source && (
                    <Meta label="Source" value={playbook.source} mono />
                  )}
                </dl>
              </div>

              {playbook.source_interaction_ids?.length > 0 && (
                <div className="rounded-lg border border-border bg-card p-4">
                  <h3 className="text-xs font-semibold uppercase text-muted-foreground mb-3">
                    Extracted from
                  </h3>
                  <p className="text-xs text-muted-foreground mb-2">
                    {playbook.source_interaction_ids.length} interaction
                    {playbook.source_interaction_ids.length === 1 ? "" : "s"}
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {playbook.source_interaction_ids.slice(0, 24).map((iid) => (
                      <Badge
                        key={iid}
                        variant="outline"
                        className="font-mono text-[10px]"
                      >
                        #{iid}
                      </Badge>
                    ))}
                    {playbook.source_interaction_ids.length > 24 && (
                      <Badge variant="ghost" className="text-[10px]">
                        +{playbook.source_interaction_ids.length - 24} more
                      </Badge>
                    )}
                  </div>
                </div>
              )}
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
      <div className="flex items-baseline gap-2">
        <Label className="text-sm font-semibold flex items-center gap-1.5">
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
          {title}
        </Label>
        {hint && (
          <span className="text-xs text-muted-foreground">{hint}</span>
        )}
      </div>
      {children}
    </section>
  );
}

function Prose({ text, muted = false }: { text: string; muted?: boolean }) {
  if (!text) {
    return (
      <p className="text-sm text-muted-foreground italic">
        {muted ? "Not set" : "—"}
      </p>
    );
  }
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card px-4 py-3",
        muted && "bg-muted/30",
      )}
    >
      <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
        {text}
      </p>
    </div>
  );
}

function AutoTextarea({
  value,
  onChange,
  rows = 3,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
}) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      placeholder={placeholder}
      className="w-full rounded-lg border border-input bg-transparent px-4 py-3 text-sm leading-relaxed font-sans resize-y outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 placeholder:text-muted-foreground"
    />
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
  label,
  value,
  display,
}: {
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
      <dt className="text-xs text-muted-foreground shrink-0">{label}</dt>
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
      learningName="this project-specific skill"
      description="Delete this claude-smart skill permanently."
      consequences={[
        "This skill will stop being injected into future sessions for this project.",
        "Historical session records may still show prior uses.",
      ]}
      onDelete={onDelete}
      deleting={deleting}
      disabled={disabled}
    />
  );
}
