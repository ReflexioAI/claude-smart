import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const DEFAULT_WARNING_BYTES = 10 * 1024 ** 3;
const ENV_KEYS = [
  "REFLEXIO_RETENTION_ARCHIVE",
  "REFLEXIO_RETENTION_ARCHIVE_DIR",
  "REFLEXIO_RETENTION_ARCHIVE_WARN_BYTES",
  "LOCAL_STORAGE_PATH",
] as const;

export interface ArchiveStatus {
  enabled: boolean;
  entryCount: number;
  sizeBytes: number;
  warningBytes: number;
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

function warningBytes(value: string | undefined): number {
  if (!value) return DEFAULT_WARNING_BYTES;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0
    ? parsed
    : DEFAULT_WARNING_BYTES;
}

function countEntries(file: string): Promise<number> {
  return new Promise((resolve, reject) => {
    let lines = 0;
    let sawBytes = false;
    let lastByte = 10;
    const stream = createReadStream(file);
    stream.on("data", (chunk) => {
      const bytes = typeof chunk === "string" ? Buffer.from(chunk) : chunk;
      if (bytes.length === 0) return;
      sawBytes = true;
      lastByte = bytes[bytes.length - 1];
      for (const byte of bytes) {
        if (byte === 10) lines += 1;
      }
    });
    stream.on("error", reject);
    stream.on("end", () => resolve(lines + (sawBytes && lastByte !== 10 ? 1 : 0)));
  });
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
  const threshold = warningBytes(values.REFLEXIO_RETENTION_ARCHIVE_WARN_BYTES);
  if (!enabled) {
    return {
      enabled: false,
      entryCount: 0,
      sizeBytes: 0,
      warningBytes: threshold,
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

  let entryCount = 0;
  let sizeBytes = 0;
  for (const file of files) {
    try {
      const [entries, stat] = await Promise.all([countEntries(file), fs.stat(file)]);
      entryCount += entries;
      sizeBytes += stat.size;
    } catch {
      // Rotation can remove a file between readdir and read; skip that file.
    }
  }
  return {
    enabled: true,
    entryCount,
    sizeBytes,
    warningBytes: threshold,
    exceeded: sizeBytes > threshold,
  };
}
