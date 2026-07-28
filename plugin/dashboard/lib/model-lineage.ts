/**
 * Direct SQLite reader for observed model/provider lineage on generated
 * learnings. Reads ~/.reflexio/data/reflexio.db (or CLAUDE_SMART_REFLEXIO_DB)
 * without going through the Reflexio HTTP API.
 *
 * Contract: only observed model_name/provider are supported going forward.
 * Historical rows may lack these columns/values; treat them as unrecorded.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

export type LearningEntityType =
  | "profile"
  | "user_playbook"
  | "agent_playbook";

export interface LearningModelProvenance {
  entityType: LearningEntityType;
  entityId: string;
  modelName: string | null;
  provider: string | null;
  op: string | null;
  actor: string | null;
  eventId: number | null;
  createdAt: number | null;
  unavailable: boolean;
  reason?: string;
}

const CONTENT_OPS = new Set(["create", "revise", "merge", "aggregate"]);

export function defaultReflexioDbPath(): string {
  const override = process.env.CLAUDE_SMART_REFLEXIO_DB;
  if (override && override.trim()) return override.trim();
  return path.join(os.homedir(), ".reflexio", "data", "reflexio.db");
}

function emptyProvenance(
  entityType: LearningEntityType,
  entityId: string,
  unavailable: boolean,
  reason?: string,
): LearningModelProvenance {
  return {
    entityType,
    entityId,
    modelName: null,
    provider: null,
    op: null,
    actor: null,
    eventId: null,
    createdAt: null,
    unavailable,
    reason,
  };
}

function normalizeEntityType(value: string): LearningEntityType | null {
  if (
    value === "profile" ||
    value === "user_playbook" ||
    value === "agent_playbook"
  ) {
    return value;
  }
  return null;
}

type LineageRow = {
  event_id: number;
  op: string | null;
  actor: string | null;
  model_name: string | null;
  provider: string | null;
  created_at: number | null;
};

function pickBestRow(rows: LineageRow[]): LineageRow | null {
  if (rows.length === 0) return null;

  const withModel = rows.filter(
    (row) =>
      (typeof row.model_name === "string" && row.model_name.trim() !== "") ||
      (typeof row.provider === "string" && row.provider.trim() !== ""),
  );
  const pool = withModel.length > 0 ? withModel : rows;

  const ranked = [...pool].sort((a, b) => {
    const aContent = CONTENT_OPS.has(a.op ?? "") ? 1 : 0;
    const bContent = CONTENT_OPS.has(b.op ?? "") ? 1 : 0;
    if (aContent !== bContent) return bContent - aContent;
    const aTs = typeof a.created_at === "number" ? a.created_at : 0;
    const bTs = typeof b.created_at === "number" ? b.created_at : 0;
    if (aTs !== bTs) return bTs - aTs;
    return (b.event_id ?? 0) - (a.event_id ?? 0);
  });
  return ranked[0] ?? null;
}

function openReadonlyDb(dbPath: string): DatabaseSync {
  // node:sqlite DatabaseSync readOnly requires Node >=22.18 / >=24.4.
  return new DatabaseSync(dbPath, { readOnly: true });
}

function tableColumns(db: DatabaseSync, table: string): Set<string> {
  const rows = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{
    name?: string;
  }>;
  return new Set(
    rows
      .map((row) => (typeof row.name === "string" ? row.name : ""))
      .filter(Boolean),
  );
}

export function getLearningModelProvenance(
  entityTypeRaw: string,
  entityIdRaw: string,
  dbPath: string = defaultReflexioDbPath(),
): LearningModelProvenance {
  const entityType = normalizeEntityType(entityTypeRaw);
  const entityId = String(entityIdRaw ?? "").trim();
  if (!entityType) {
    return emptyProvenance(
      "profile",
      entityId,
      true,
      `unsupported entityType: ${entityTypeRaw}`,
    );
  }
  if (!entityId) {
    return emptyProvenance(entityType, entityId, true, "missing entityId");
  }
  if (!fs.existsSync(dbPath)) {
    // Keep host path details server-side only; UI gets a stable public reason.
    console.error(`[model-lineage] database unavailable at ${dbPath}`);
    return emptyProvenance(entityType, entityId, true, "database unavailable");
  }

  let db: DatabaseSync | null = null;
  try {
    db = openReadonlyDb(dbPath);
    const columns = tableColumns(db, "lineage_event");
    if (columns.size === 0) {
      return emptyProvenance(
        entityType,
        entityId,
        true,
        "lineage_event table not present",
      );
    }

    const hasModelName = columns.has("model_name");
    const hasProvider = columns.has("provider");
    const selectParts = [
      "event_id",
      "op",
      "actor",
      "created_at",
      hasModelName ? "model_name" : "NULL AS model_name",
      hasProvider ? "provider" : "NULL AS provider",
    ];

    const stmt = db.prepare(
      `SELECT ${selectParts.join(", ")}
         FROM lineage_event
        WHERE entity_type = ?
          AND entity_id = ?
        ORDER BY created_at DESC, event_id DESC
        LIMIT 50`,
    );
    const rows = stmt.all(entityType, entityId) as LineageRow[];
    const best = pickBestRow(rows);
    if (!best) {
      return emptyProvenance(
        entityType,
        entityId,
        false,
        "no lineage events for this learning",
      );
    }

    const modelName =
      typeof best.model_name === "string" && best.model_name.trim()
        ? best.model_name.trim()
        : null;
    const provider =
      typeof best.provider === "string" && best.provider.trim()
        ? best.provider.trim()
        : null;

    return {
      entityType,
      entityId,
      modelName,
      provider,
      op: best.op ?? null,
      actor: best.actor ?? null,
      eventId: typeof best.event_id === "number" ? best.event_id : null,
      createdAt: typeof best.created_at === "number" ? best.created_at : null,
      unavailable: false,
      reason:
        modelName || provider
          ? undefined
          : "lineage present but observed model/provider not recorded",
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    // Keep SQLite diagnostics server-side only.
    console.error(`[model-lineage] provenance query failed: ${message}`);
    return emptyProvenance(
      entityType,
      entityId,
      true,
      "provenance query failed",
    );
  } finally {
    try {
      db?.close();
    } catch {
      // ignore close failures
    }
  }
}
