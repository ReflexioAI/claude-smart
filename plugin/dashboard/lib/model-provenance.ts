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

  useEffect(() => {
    if (!ready || !entityType) {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5_000);
    const params = new URLSearchParams({
      entityType,
      entityId: String(entityId),
    });

    fetch(`/api/learning-model-provenance?${params.toString()}`, {
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
            requestedModel: null,
            credentialLabel: null,
            op: null,
            actor: null,
            eventId: null,
            createdAt: null,
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
  }, [ready, entityType, entityId]);

  return ready ? provenance : null;
}
