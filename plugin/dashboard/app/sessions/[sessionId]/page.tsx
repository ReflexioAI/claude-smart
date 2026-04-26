"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Wrench,
  AlertTriangle,
  ChevronRight,
  Trash2,
  Clock,
  FolderGit2,
  Copy,
  Check,
  Sparkles,
} from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { EmptyState } from "@/components/common/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { formatTimestamp, truncateId } from "@/lib/format";
import type { CitedItem, SessionDetail } from "@/lib/types";

export default function InteractionDetailPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = use(params);
  const router = useRouter();
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const remove = async () => {
    if (!confirm(`Delete session ${sessionId}? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(sessionId)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`delete failed: ${res.status}`);
      router.push("/sessions");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`failed: ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Session transcript"
        description={sessionId}
        actions={
          <div className="flex items-center gap-2">
            <Link href="/sessions">
              <Button variant="outline" size="sm">
                <ArrowLeft className="h-3.5 w-3.5" />
                Back
              </Button>
            </Link>
            <Button
              variant="destructive"
              size="sm"
              onClick={remove}
              disabled={deleting}
            >
              <Trash2 className="h-3.5 w-3.5" />
              {deleting ? "Deleting…" : "Delete"}
            </Button>
          </div>
        }
      />

      <div className="p-6 max-w-3xl mx-auto">
        {error && (
          <EmptyState
            icon={AlertTriangle}
            title="Unable to load session"
            description={error}
          />
        )}

        {!error && detail && detail.turns.length === 0 && (
          <EmptyState
            icon={AlertTriangle}
            title="Empty session"
            description="No turns recorded in this buffer yet."
          />
        )}

        {detail && detail.turns.length > 0 && (
          <div className="space-y-4">
            {detail.turns.map((turn, idx) => {
              const isUser = turn.role === "User";
              const flagged =
                turn.user_action && turn.user_action !== "NONE";
              return (
                <article
                  key={idx}
                  className={cn(
                    "rounded-xl border px-4 py-3 bg-card",
                    flagged
                      ? "border-destructive/30 bg-destructive/5"
                      : "border-border",
                  )}
                >
                  <header className="flex items-center justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={isUser ? "secondary" : "outline"}
                        className="h-5 font-mono text-[10px]"
                      >
                        {turn.role}
                      </Badge>
                      {flagged && (
                        <Badge variant="destructive" className="h-5">
                          {turn.user_action}
                        </Badge>
                      )}
                      {turn.tools_used && turn.tools_used.length > 0 && (
                        <div className="flex items-center gap-1 flex-wrap">
                          {turn.tools_used.map((t, ti) => {
                            const input = t.tool_data?.input;
                            const output = t.tool_data?.output;
                            const hasInput =
                              input && Object.keys(input).length > 0;
                            const hasOutput =
                              typeof output === "string" && output.length > 0;
                            if (!hasInput && !hasOutput) {
                              return (
                                <span key={ti}>
                                  <Badge
                                    variant="outline"
                                    className="h-5 gap-1 text-[10px]"
                                  >
                                    <Wrench className="h-3 w-3" />
                                    {t.tool_name}
                                  </Badge>
                                </span>
                              );
                            }
                            return (
                              <details key={ti} className="group">
                                <summary className="cursor-pointer list-none">
                                  <Badge
                                    variant="outline"
                                    className="h-5 gap-1 text-[10px]"
                                  >
                                    <Wrench className="h-3 w-3" />
                                    {t.tool_name}
                                    <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
                                  </Badge>
                                </summary>
                                <div className="mt-1 space-y-1.5">
                                  {hasInput && (
                                    <div>
                                      <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground/70 mb-0.5">
                                        input
                                      </div>
                                      <pre className="whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 px-2 py-1 text-[10px] font-mono text-muted-foreground">
                                        {JSON.stringify(input, null, 2)}
                                      </pre>
                                    </div>
                                  )}
                                  {hasOutput && (
                                    <div>
                                      <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground/70 mb-0.5">
                                        output
                                      </div>
                                      <pre className="whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 px-2 py-1 text-[10px] font-mono text-muted-foreground">
                                        {output}
                                      </pre>
                                    </div>
                                  )}
                                </div>
                              </details>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  </header>
                  <pre className="whitespace-pre-wrap break-words text-sm font-sans leading-relaxed">
                    {turn.content}
                  </pre>
                  {turn.user_action_description && (
                    <p className="text-xs text-muted-foreground mt-2 italic">
                      {turn.user_action_description}
                    </p>
                  )}
                  {turn.cited_items && turn.cited_items.length > 0 && (
                    <CitedItemsRow items={turn.cited_items} />
                  )}
                  <TurnMeta ts={turn.ts} userId={turn.user_id} />
                </article>
              );
            })}
            <div className="text-xs text-muted-foreground text-center">
              Published up to turn {detail.published_up_to}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function CitedItemsRow({ items }: { items: CitedItem[] }) {
  return (
    <div className="mt-3 flex items-start gap-2 text-[11px]">
      <Sparkles className="h-3.5 w-3.5 mt-0.5 text-amber-500 shrink-0" />
      <div className="flex items-center gap-1 flex-wrap">
        <span className="text-muted-foreground">Used</span>
        {items.map((item) => {
          const targetId = item.real_id ?? item.id;
          const href =
            item.kind === "playbook"
              ? `/playbooks/${encodeURIComponent(targetId)}`
              : `/profiles/${encodeURIComponent(targetId)}`;
          return (
            <Link
              key={item.id}
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              title={`${item.kind} • id=${targetId}`}
            >
              <Badge
                variant="outline"
                className="h-5 gap-1 text-[10px] border-amber-500/40 cursor-pointer hover:bg-amber-500/10 hover:border-amber-500/70 transition-colors"
              >
                {item.title || item.id}
              </Badge>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

function TurnMeta({ ts, userId }: { ts?: number; userId?: string }) {
  if (ts === undefined && !userId) return null;
  return (
    <dl className="mt-3 pt-2 border-t border-border/60 flex items-center justify-end gap-4 text-[11px]">
      {ts !== undefined && (
        <div className="flex items-center gap-1.5">
          <dt className="text-muted-foreground flex items-center gap-1">
            <Clock className="h-3 w-3" />
          </dt>
          <dd className="font-mono text-muted-foreground">
            {formatTimestamp(ts)}
          </dd>
        </div>
      )}
      {userId && (
        <div className="flex items-center gap-1.5">
          <dt className="text-muted-foreground flex items-center gap-1">
            <FolderGit2 className="h-3 w-3" />
            <span>Project</span>
          </dt>
          <dd className="flex items-center gap-1">
            <code className="font-mono">{truncateId(userId, 32, 8)}</code>
            <CopyButton value={userId} />
          </dd>
        </div>
      )}
    </dl>
  );
}

function CopyButton({ value }: { value: string }) {
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
    <button
      onClick={copy}
      className="text-muted-foreground hover:text-foreground transition-colors"
      title="Copy"
    >
      {copied ? (
        <Check className="h-3 w-3 text-emerald-500" />
      ) : (
        <Copy className="h-3 w-3" />
      )}
    </button>
  );
}
