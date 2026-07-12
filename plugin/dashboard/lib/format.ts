export function formatTimestamp(ts: number | null | undefined): string {
  if (!ts) return "—";
  const ms = ts < 1e12 ? ts * 1000 : ts;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatRelative(ts: number | null | undefined): string {
  if (!ts) return "—";
  const ms = ts < 1e12 ? ts * 1000 : ts;
  const diff = Date.now() - ms;
  if (diff < 0) return "just now";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  if (days < 30) return `${days}d ago`;
  return formatTimestamp(ts);
}

export function truncate(text: string, n: number): string {
  if (text.length <= n) return text;
  return text.slice(0, n - 1).trimEnd() + "…";
}

export function truncateId(id: string, prefix = 8, suffix = 4): string {
  if (id.length <= prefix + suffix + 1) return id;
  return `${id.slice(0, prefix)}…${id.slice(-suffix)}`;
}

export function dayBucket(ts: number | null | undefined): string {
  if (!ts) return "Unknown";
  const ms = ts < 1e12 ? ts * 1000 : ts;
  const d = new Date(ms);
  const now = new Date();
  const startOfToday = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const oneDay = 86_400_000;
  if (d.getTime() >= startOfToday) return "Today";
  if (d.getTime() >= startOfToday - oneDay) return "Yesterday";
  if (d.getTime() >= startOfToday - 7 * oneDay) return "Earlier this week";
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString(undefined, { month: "long" });
  }
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long" });
}
