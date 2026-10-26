// Kept free of imports so tests can load it with plain node.

/**
 * Provenance is read from the local backend's SQLite lineage, so it exists
 * only for the local data source. A managed entity id names a managed row and
 * must never be looked up locally (it would miss, or match another learning).
 */
export function modelProvenanceAvailable(source: string | null): boolean {
  return source === "local";
}

export function provenanceRequestKey(
  source: string | null,
  entityType: string | null,
  entityId: string | number | null | undefined,
): string | null {
  if (!modelProvenanceAvailable(source) || !entityType) return null;
  if (entityId === null || entityId === undefined || entityId === "")
    return null;
  return `${entityType}:${String(entityId)}`;
}
