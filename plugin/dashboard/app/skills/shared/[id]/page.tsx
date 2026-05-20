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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { reflexio } from "@/lib/reflexio-client";
import { useSettings } from "@/hooks/use-settings";
import { formatTimestamp, truncateId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { agentPlaybookStatusLabel, statusLabel } from "@/lib/status";
import type { AgentPlaybook, AgentPlaybookStatus } from "@/lib/types";

type FormState = {
  content: string;
  trigger: string;
  rationale: string;
  playbookStatus: AgentPlaybookStatus;
};

function toForm(p: AgentPlaybook): FormState {
  return {
    content: p.content,
    trigger: p.trigger ?? "",
    rationale: p.rationale ?? "",
    playbookStatus: p.playbook_status,
  };
}

function displayName(name: string | null | undefined): string | null {
  if (!name) return null;
  if (name === "default_playbook_extractor") return "shared skill";
  return name;
}

const REVIEW_STATUS_META: Record<
  AgentPlaybookStatus,
  { label: string; description: string }
> = {
  pending: {
    label: "Auto generated",
    description: "Auto-generated shared skill. It may be updated automatically.",
  },
  approved: {
    label: "Persisted",
    description: "Persisted shared skill. It will not be auto updated.",
  },
  rejected: {
    label: "Rejected",
    description: "Rejected shared skill. It will not be used in claude-smart.",
  },
};

export default function SharedSkillDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const { reflexioUrl } = useSettings();

  const [playbook, setPlaybook] = useState<AgentPlaybook | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [reviewingStatus, setReviewingStatus] =
    useState<AgentPlaybookStatus | null>(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>({
    content: "",
    trigger: "",
    rationale: "",
    playbookStatus: "pending",
  });

  useEffect(() => {
    let cancelled = false;
    reflexio
      .getAgentPlaybooks({ reflexioUrl })
      .then((res) => {
        if (cancelled) return;
        const found = (res.agent_playbooks ?? []).find(
          (p) => String(p.agent_playbook_id) === id,
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
      orig.rationale !== form.rationale ||
      orig.playbookStatus !== form.playbookStatus
    );
  }, [playbook, form]);

  const save = async () => {
    if (!playbook || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      await reflexio.updateAgentPlaybook(
        {
          agent_playbook_id: playbook.agent_playbook_id,
          content: form.content,
          trigger: form.trigger || null,
          rationale: form.rationale || null,
          playbook_status: form.playbookStatus,
        },
        reflexioUrl,
      );
      setPlaybook({
        ...playbook,
        content: form.content,
        trigger: form.trigger || null,
        rationale: form.rationale || null,
        playbook_status: form.playbookStatus,
      });
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const setReviewStatus = async (nextStatus: AgentPlaybookStatus) => {
    if (!playbook || playbook.playbook_status === nextStatus) return;
    if (
      nextStatus === "rejected" &&
      !confirm(
        `Reject shared skill #${playbook.agent_playbook_id}? Rejected shared skills will not be used in claude-smart.`,
      )
    ) {
      return;
    }

    setReviewingStatus(nextStatus);
    setError(null);
    try {
      await reflexio.updateAgentPlaybook(
        {
          agent_playbook_id: playbook.agent_playbook_id,
          playbook_status: nextStatus,
        },
        reflexioUrl,
      );
      setPlaybook((current) =>
        current ? { ...current, playbook_status: nextStatus } : current,
      );
      setForm((current) => ({ ...current, playbookStatus: nextStatus }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setReviewingStatus(null);
    }
  };

  const remove = async () => {
    if (!playbook) return;
    setDeleting(true);
    try {
      await reflexio.deleteAgentPlaybook(playbook.agent_playbook_id, reflexioUrl);
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
        <PageHeader title="Shared skill not found" />
        <div className="p-6 max-w-2xl mx-auto">
          <EmptyState
            icon={AlertTriangle}
            title="Shared skill not found"
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

  const lifecycleStatus = playbook ? statusLabel(playbook) : null;
  const playbookStatus = playbook ? agentPlaybookStatusLabel(playbook) : null;

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title={`Shared skill #${playbook?.agent_playbook_id ?? id}`}
        description="Shared skill rolled up from project-specific skills."
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
                disabled={!playbook || reviewingStatus !== null}
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
              <div className="rounded-xl border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm flex items-start gap-2">
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
                {editing ? (
                  <ReviewStatusBadge
                    status={playbook.playbook_status}
                    displayStatus={playbookStatus!}
                  />
                ) : (
                  <ReviewStatusSelect
                    status={playbook.playbook_status}
                    displayStatus={playbookStatus!}
                    disabled={reviewingStatus !== null || deleting}
                    busy={reviewingStatus !== null}
                    onChange={setReviewStatus}
                  />
                )}
                {lifecycleStatus !== "CURRENT" && (
                  <StatusBadge status={lifecycleStatus!} />
                )}
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
              hint="When this shared skill should apply. Leave empty if it always applies."
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
              icon={Check}
              title="Review status"
              hint="Auto generated, persisted, or rejected."
            >
              {editing ? (
                <Select
                  value={form.playbookStatus}
                  onValueChange={(v) =>
                    setForm((f) => ({
                      ...f,
                      playbookStatus: v as AgentPlaybookStatus,
                    }))
                  }
                >
                  <SelectTrigger
                    className="w-48 text-xs"
                    title={REVIEW_STATUS_META[form.playbookStatus].description}
                  >
                    <SelectValue placeholder="Status" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem
                      value="pending"
                      title={REVIEW_STATUS_META.pending.description}
                    >
                      {REVIEW_STATUS_META.pending.label}
                    </SelectItem>
                    <SelectItem
                      value="approved"
                      title={REVIEW_STATUS_META.approved.description}
                    >
                      {REVIEW_STATUS_META.approved.label}
                    </SelectItem>
                    <SelectItem
                      value="rejected"
                      title={REVIEW_STATUS_META.rejected.description}
                    >
                      {REVIEW_STATUS_META.rejected.label}
                    </SelectItem>
                  </SelectContent>
                </Select>
              ) : (
                playbook && (
                  <ReviewStatusSelect
                    status={playbook.playbook_status}
                    displayStatus={playbookStatus ?? "PENDING"}
                    disabled={reviewingStatus !== null || deleting}
                    busy={reviewingStatus !== null}
                    onChange={setReviewStatus}
                  />
                )
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
              <div className="rounded-xl border border-border bg-card p-4">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3">
                  Metadata
                </h3>
                <dl className="space-y-2.5 text-sm">
                  <Meta
                    icon={Hash}
                    label="ID"
                    value={String(playbook.agent_playbook_id)}
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
                  <Meta
                    label="Review"
                    value={REVIEW_STATUS_META[playbook.playbook_status].label}
                    mono
                  />
                  {playbook.playbook_metadata && (
                    <CopyMeta
                      label="Metadata"
                      value={playbook.playbook_metadata}
                      display={truncateId(playbook.playbook_metadata, 32, 8)}
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
        "rounded-xl border border-border bg-card px-4 py-3",
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
      className="w-full rounded-xl border border-input bg-transparent px-4 py-3 text-sm leading-relaxed font-sans resize-y outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 placeholder:text-muted-foreground"
    />
  );
}

function StatusBadge({
  status,
}: {
  status: "CURRENT" | "ARCHIVED" | "PENDING" | "APPROVED" | "REJECTED";
}) {
  const variant =
    status === "CURRENT" || status === "APPROVED"
      ? "secondary"
      : status === "ARCHIVED" || status === "REJECTED"
        ? "outline"
        : "default";
  return (
    <Badge variant={variant} className="gap-1.5">
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          status === "CURRENT" && "bg-emerald-500",
          status === "APPROVED" && "bg-emerald-500",
          status === "PENDING" && "bg-amber-500",
          status === "REJECTED" && "bg-destructive",
          status === "ARCHIVED" && "bg-muted-foreground",
        )}
      />
      {status}
    </Badge>
  );
}

function ReviewStatusSelect({
  status,
  displayStatus,
  disabled,
  busy,
  onChange,
}: {
  status: AgentPlaybookStatus;
  displayStatus: "PENDING" | "APPROVED" | "REJECTED";
  disabled: boolean;
  busy: boolean;
  onChange: (status: AgentPlaybookStatus) => void;
}) {
  const meta = REVIEW_STATUS_META[status];

  return (
    <Select
      value={status}
      onValueChange={(value) => onChange(value as AgentPlaybookStatus)}
      disabled={disabled}
    >
      <SelectTrigger
        size="sm"
        aria-label="Review status"
        title={meta.description}
        className={cn(
          "h-7 w-fit gap-1.5 rounded-lg border px-2.5 py-0 text-xs font-medium",
          "bg-background hover:bg-muted focus-visible:ring-3",
          displayStatus === "APPROVED" &&
            "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
          displayStatus === "PENDING" &&
            "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
          displayStatus === "REJECTED" &&
            "border-destructive/30 bg-destructive/10 text-destructive",
        )}
      >
        <StatusPillContent
          status={displayStatus}
          label={busy ? "Updating" : meta.label}
        />
      </SelectTrigger>
      <SelectContent align="start" alignItemWithTrigger={false}>
        <SelectItem
          value="pending"
          title={REVIEW_STATUS_META.pending.description}
        >
          <StatusPillContent
            status="PENDING"
            label={REVIEW_STATUS_META.pending.label}
          />
        </SelectItem>
        <SelectItem
          value="approved"
          title={REVIEW_STATUS_META.approved.description}
        >
          <StatusPillContent
            status="APPROVED"
            label={REVIEW_STATUS_META.approved.label}
          />
        </SelectItem>
        <SelectItem
          value="rejected"
          title={REVIEW_STATUS_META.rejected.description}
        >
          <StatusPillContent
            status="REJECTED"
            label={REVIEW_STATUS_META.rejected.label}
          />
        </SelectItem>
      </SelectContent>
    </Select>
  );
}

function ReviewStatusBadge({
  status,
  displayStatus,
}: {
  status: AgentPlaybookStatus;
  displayStatus: "PENDING" | "APPROVED" | "REJECTED";
}) {
  const meta = REVIEW_STATUS_META[status];
  return (
    <Badge
      variant={displayStatus === "REJECTED" ? "outline" : "secondary"}
      className={cn(
        "gap-1.5",
        displayStatus === "APPROVED" &&
          "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        displayStatus === "PENDING" &&
          "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
        displayStatus === "REJECTED" &&
          "border-destructive/30 bg-destructive/10 text-destructive",
      )}
      title={meta.description}
    >
      <StatusPillContent status={displayStatus} label={meta.label} />
    </Badge>
  );
}

function StatusPillContent({
  status,
  label,
}: {
  status: "PENDING" | "APPROVED" | "REJECTED";
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          status === "APPROVED" && "bg-emerald-500",
          status === "PENDING" && "bg-amber-500",
          status === "REJECTED" && "bg-destructive",
        )}
      />
      {label}
    </span>
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
      learningName="this shared skill"
      description="Delete this shared claude-smart skill permanently."
      consequences={[
        "This shared skill will stop being available to claude-smart.",
        "Historical session records may still show prior uses.",
      ]}
      onDelete={onDelete}
      deleting={deleting}
      disabled={disabled}
    />
  );
}
