import { useEffect, useState } from "react";
import type {
  LearningEntityType,
  LearningModelProvenance,
} from "./model-lineage";

export type { LearningEntityType, LearningModelProvenance };

export function useLearningModelProvenance(
  entityType: LearningEntityType | null,
  entityId: string | number | null | undefined,
): LearningModelProvenance | null {
  const [provenance, setProvenance] = useState<LearningModelProvenance | null>(
    null,
  );
  const ready =
    !!entityType &&
    entityId !== null &&
    entityId !== undefined &&
    entityId !== "";
  const requestKey = ready ? `${entityType}:${String(entityId)}` : null;

  useEffect(() => {
    if (!requestKey || !entityType) return;

    let cancelled = false;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5_000);
    const params = new URLSearchParams({
      entityType,
      entityId: String(entityId),
    });

    fetch(`/api/model-provenance?${params.toString()}`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`model provenance ${response.status}`);
        const data = await response.json();
        return data.provenance as LearningModelProvenance;
      })
      .then((value) => {
        if (!cancelled) setProvenance(value);
      })
      .catch(() => {
        if (!cancelled) {
          setProvenance({
            entityType,
            entityId: String(entityId),
            modelName: null,
            provider: null,
            unavailable: true,
            reason: "failed to load model provenance",
          });
        }
      })
      .finally(() => clearTimeout(timeout));

    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timeout);
    };
  }, [requestKey, entityType, entityId]);

  // Drop stale results immediately when the target entity changes so the UI
  // shows Loading… instead of the previous learning's model.
  if (!requestKey) return null;
  if (
    provenance &&
    `${provenance.entityType}:${provenance.entityId}` !== requestKey
  ) {
    return null;
  }
  return provenance;
}
