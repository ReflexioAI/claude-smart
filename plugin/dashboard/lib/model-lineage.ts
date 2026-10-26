/**
 * Read observed model/provider from local Reflexio SQLite lineage.
 * Direct DB access only — not the Reflexio HTTP API.
 */

import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";

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

type SqliteDatabase = {
  prepare: (sql: string) => {
    all: (...params: unknown[]) => unknown[];
    get: (...params: unknown[]) => unknown;
  };
  close: () => void;
};

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

// claude-smart's backend runs under this org id (backend-service.sh hard-sets
// REFLEXIO_DEFAULT_ORG_ID), which names its config file and per-org database.
const CLAUDE_SMART_ORG_ID = "claude-smart";

/** Mirror of reflexio.cli.paths.reflexio_home(). */
function reflexioHome(env: NodeJS.ProcessEnv): string {
  const base = env.REFLEXIO_LOG_DIR;
  if (!base) return path.join(os.homedir(), ".reflexio");
  const expanded = base.replace(/^~(?=$|[\\/])/, os.homedir());
  return path.join(path.resolve(os.homedir(), expanded), ".reflexio");
}

/**
 * The SQLite file claude-smart's local backend stores lineage in, or null when
 * its local storage is not SQLite.
 *
 * The org config's `storage_config.db_path` is authoritative: the backend
 * runner (plugin/scripts/backend-python-runner.py) pins it on every start.
 * When it is unset, resolve it the way that runner does: the legacy
 * `reflexio.db`, unless only the per-org `reflexio_<org>.db` exists.
 */
export function resolveLocalReflexioDbPath(
  env: NodeJS.ProcessEnv = process.env,
): string | null {
  const home = reflexioHome(env);
  const configFile = path.join(
    home,
    "configs",
    `config_${CLAUDE_SMART_ORG_ID}.json`,
  );
  let storage: unknown;
  try {
    storage = JSON.parse(fs.readFileSync(configFile, "utf8"))?.storage_config;
  } catch {
    storage = undefined;
  }
  if (storage && typeof storage === "object") {
    const configured = (storage as Record<string, unknown>).db_path;
    if (typeof configured === "string" && configured.trim())
      return configured.trim();
    // StorageConfigSQLite is the only storage config carrying db_path.
    if (!("db_path" in storage)) return null;
  }
  const root = env.LOCAL_STORAGE_PATH?.trim() || path.join(home, "data");
  const legacy = path.join(root, "reflexio.db");
  const derived = path.join(root, `reflexio_${CLAUDE_SMART_ORG_ID}.db`);
  return fs.existsSync(derived) && !fs.existsSync(legacy) ? derived : legacy;
}

function openDatabase(sqlitePath: string): SqliteDatabase {
  // Lazy-load so module evaluation stays safe on Node versions that cannot
  // import node:sqlite at top level during install/build matrices.
  // Use a real filesystem path for createRequire: Next may rewrite
  // import.meta.url into a URL object that breaks CommonJS resolution.
  const req = createRequire(path.join(process.cwd(), "package.json"));
  const sqlite = req("node:sqlite") as {
    DatabaseSync: new (
      filename: string,
      options?: { readOnly?: boolean },
    ) => SqliteDatabase;
  };
  try {
    return new sqlite.DatabaseSync(sqlitePath, { readOnly: true });
  } catch {
    // Older Node builds may lack the readOnly option; fall back to default open.
    return new sqlite.DatabaseSync(sqlitePath);
  }
}

function columns(db: SqliteDatabase, table: string): Set<string> {
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
  sqlitePath: string | null = resolveLocalReflexioDbPath(),
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
  if (!sqlitePath) {
    return empty(entityType, entityId, true, "local storage is not SQLite");
  }
  if (!fs.existsSync(sqlitePath)) {
    console.error(`[model-lineage] database unavailable at ${sqlitePath}`);
    return empty(entityType, entityId, true, "database unavailable");
  }

  let db: SqliteDatabase | null = null;
  try {
    db = openDatabase(sqlitePath);
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
