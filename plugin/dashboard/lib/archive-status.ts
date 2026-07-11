import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const DEFAULT_WARNING_BYTES = 10 * 1024 ** 3;
const ENV_KEYS = [
  "REFLEXIO_RETENTION_ARCHIVE",
  "REFLEXIO_RETENTION_ARCHIVE_DIR",
  "REFLEXIO_RETENTION_ARCHIVE_MAX_BYTES",
  "LOCAL_STORAGE_PATH",
] as const;

export interface ArchiveStatus {
  enabled: boolean;
  sizeBytes: number;
  maxBytes: number;
  exceeded: boolean;
}

function parseEnv(text: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator < 0) continue;
    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    values[key] = value;
  }
  return values;
}

async function readEnv(file: string): Promise<Record<string, string>> {
  try {
    return parseEnv(await fs.readFile(file, "utf-8"));
  } catch {
    return {};
  }
}

function isTruthy(value: string | undefined): boolean {
  return value === "1" || value?.toLowerCase() === "true";
}

function maxBytes(value: string | undefined): number {
  if (!value) return DEFAULT_WARNING_BYTES;
  if (!/^[+-]?\d+$/.test(value)) return DEFAULT_WARNING_BYTES;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0
    ? parsed
    : DEFAULT_WARNING_BYTES;
}

export async function readArchiveStatus(): Promise<ArchiveStatus> {
  const home = os.homedir();
  const reflexioEnv = await readEnv(path.join(home, ".reflexio", ".env"));
  const claudeSmartEnv = await readEnv(path.join(home, ".claude-smart", ".env"));
  const values = { ...reflexioEnv, ...claudeSmartEnv };
  for (const key of ENV_KEYS) {
    if (process.env[key] !== undefined) values[key] = process.env[key];
  }

  const enabled = isTruthy(values.REFLEXIO_RETENTION_ARCHIVE);
  const ceiling = maxBytes(values.REFLEXIO_RETENTION_ARCHIVE_MAX_BYTES);
  if (!enabled) {
    return {
      enabled: false,
      sizeBytes: 0,
      maxBytes: ceiling,
      exceeded: false,
    };
  }

  const archiveDir =
    values.REFLEXIO_RETENTION_ARCHIVE_DIR ||
    path.join(
      values.LOCAL_STORAGE_PATH || path.join(home, ".reflexio", "data"),
      "archive",
    );
  let files: string[] = [];
  try {
    files = (await fs.readdir(archiveDir))
      .filter((name) => name.endsWith(".jsonl"))
      .map((name) => path.join(archiveDir, name));
  } catch {
    files = [];
  }

  let sizeBytes = 0;
  for (const file of files) {
    try {
      sizeBytes += (await fs.stat(file)).size;
    } catch {
      // Rotation can remove a file between readdir and read; skip that file.
    }
  }
  return {
    enabled: true,
    sizeBytes,
    maxBytes: ceiling,
    exceeded: sizeBytes >= ceiling,
  };
}
