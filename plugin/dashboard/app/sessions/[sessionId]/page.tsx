"use client";
import { use, useEffect, useState } from "react";
import Link from "@/components/common/source-link";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { reflexio } from "@/lib/reflexio-client";
import type { Interaction } from "@/lib/types";
export default function TranscriptPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = use(params);
  const [items, setItems] = useState<Interaction[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    reflexio
      .getTranscript(sessionId)
      .then((data) => {
        if (!cancelled) {
          setItems(data.items);
          setCursor(data.next_cursor || null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e.message || e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);
  const more = async () => {
    if (!cursor) return;
    setLoading(true);
    setError(null);
    try {
      const data = await reflexio.getTranscript(sessionId, cursor);
      setItems((current) => [...current, ...data.items]);
      setCursor(data.next_cursor || null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };
  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Backend transcript"
        description={sessionId}
        actions={<Link href="/sessions">Back to requests</Link>}
      />
      <div className="p-6 space-y-4">
        {error && (
          <p role="alert" className="text-destructive">
            {error}
          </p>
        )}
        {items.map((item) => (
          <article
            key={item.interaction_id}
            className="border-b border-border py-3"
          >
            <h2 className="text-sm font-semibold">{item.role}</h2>
            <p className="text-sm whitespace-pre-wrap mt-2">{item.content}</p>
          </article>
        ))}
        {loading && <p role="status">Loading transcript…</p>}
        {!loading && !error && !items.length && (
          <p>No interactions found for this request.</p>
        )}
        {cursor && (
          <Button disabled={loading} onClick={more}>
            Load more interactions
          </Button>
        )}
      </div>
    </div>
  );
}
