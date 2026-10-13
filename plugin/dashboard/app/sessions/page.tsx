"use client";
import { useEffect, useState } from "react";
import Link from "@/components/common/source-link";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { reflexio, type RequestEntry } from "@/lib/reflexio-client";
import { useSettings } from "@/hooks/use-settings";
export default function SessionsPage() {
  const { source } = useSettings();
  const [items, setItems] = useState<RequestEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    reflexio
      .getRequests()
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
  }, []);
  const more = async () => {
    if (!cursor) return;
    setLoading(true);
    setError(null);
    try {
      const data = await reflexio.getRequests(cursor);
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
        title="Sessions · published requests"
        description={`Requests stored in ${source === "managed" ? "managed" : "local"} Reflexio. Device session buffers are listed separately.`}
      />
      <div className="p-6 space-y-4">
        {error && (
          <p role="alert" className="text-destructive">
            Unable to load requests: {error}
          </p>
        )}
        {items.map((item) => (
          <Link
            key={item.request.request_id}
            href={`/sessions/${encodeURIComponent(item.request.request_id)}`}
            className="block border-b border-border py-3"
          >
            <div className="text-sm font-medium">{item.request.request_id}</div>
            <div className="text-sm text-muted-foreground truncate">
              {item.preview}
            </div>
            <div className="text-xs text-muted-foreground">
              {item.interaction_count} interactions
            </div>
          </Link>
        ))}
        {loading && <p role="status">Loading requests…</p>}
        {!loading && !error && !items.length && (
          <p>No published requests found.</p>
        )}
        {cursor && (
          <Button disabled={loading} onClick={more}>
            Load more
          </Button>
        )}
        <Link href="/device/sessions" className="block text-sm text-primary">
          View this device’s session buffers
        </Link>
      </div>
    </div>
  );
}
