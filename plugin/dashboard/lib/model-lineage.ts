/**
 * Read observed model/provider from local Reflexio SQLite lineage.
 * Direct DB access only — not the Reflexio HTTP API.
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
  unavailable: boolean;
  reason?: string;
}

function empty(
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
    unavailable,
    reason,
  };
}

function dbPath(): string {
  const override = process.env.CLAUDE_SMART_REFLEXIO_DB?.trim();
  if (override) return override;
  return path.join(os.homedir(), ".reflexio", "data", "reflexio.db");
}

function columns(db: DatabaseSync, table: string): Set<string> {
  const rows = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{
    name?: string;
  }>;
  return new Set(
    rows.map((row) => row.name).filter((name): name is string => !!name),
  );
}

function nonEmpty(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function getLearningModelProvenance(
  entityTypeRaw: string,
  entityIdRaw: string,
  sqlitePath: string = dbPath(),
): LearningModelProvenance {
  const entityType = entityTypeRaw as LearningEntityType;
  const entityId = String(entityIdRaw ?? "").trim();
  if (
    entityType !== "profile" &&
    entityType !== "user_playbook" &&
    entityType !== "agent_playbook"
  ) {
    return empty("profile", entityId, true, "unsupported entityType");
  }
  if (!entityId) {
    return empty(entityType, entityId, true, "missing entityId");
  }
  if (!fs.existsSync(sqlitePath)) {
    console.error(`[model-lineage] database unavailable at ${sqlitePath}`);
    return empty(entityType, entityId, true, "database unavailable");
  }

  let db: DatabaseSync | null = null;
  try {
    // node:sqlite DatabaseSync readOnly requires Node >=22.18 / >=24.4.
    db = new DatabaseSync(sqlitePath, { readOnly: true });
    const cols = columns(db, "lineage_event");
    if (cols.size === 0) {
      return empty(entityType, entityId, true, "lineage_event table not present");
    }

    const modelExpr = cols.has("model_name") ? "model_name" : "NULL";
    const providerExpr = cols.has("provider") ? "provider" : "NULL";
    const row = db
      .prepare(
        `SELECT ${modelExpr} AS model_name, ${providerExpr} AS provider
           FROM lineage_event
          WHERE entity_type = ?
            AND entity_id = ?
          ORDER BY
            CASE
              WHEN COALESCE(${modelExpr}, '') != '' OR COALESCE(${providerExpr}, '') != ''
              THEN 0 ELSE 1
            END,
            created_at DESC,
            event_id DESC
          LIMIT 1`,
      )
      .get(entityType, entityId) as
      | { model_name?: unknown; provider?: unknown }
      | undefined;

    if (!row) {
      return empty(
        entityType,
        entityId,
        false,
        "no lineage events for this learning",
      );
    }

    const modelName = nonEmpty(row.model_name);
    const provider = nonEmpty(row.provider);
    return {
      entityType,
      entityId,
      modelName,
      provider,
      unavailable: false,
      reason:
        modelName || provider
          ? undefined
          : "lineage present but observed model/provider not recorded",
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[model-lineage] provenance query failed: ${message}`);
    return empty(entityType, entityId, true, "provenance query failed");
  } finally {
    try {
      db?.close();
    } catch {
      // ignore close failures
    }
  }
}
