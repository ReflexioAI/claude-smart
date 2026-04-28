// Wire status comes from Python's Status StrEnum: lowercase strings
// ("archived", "pending"). CURRENT rows are omitted from JSON entirely
// (response_model_exclude_none) and arrive as `null`/undefined.
// Normalize to the uppercase label the dashboard renders.

export type StatusLabel = "CURRENT" | "ARCHIVED" | "PENDING";

export function statusLabel(p: { status?: string | null }): StatusLabel {
  if (!p.status) return "CURRENT";
  const s = String(p.status).toLowerCase();
  if (s === "archived") return "ARCHIVED";
  if (s === "pending") return "PENDING";
  return "CURRENT";
}
