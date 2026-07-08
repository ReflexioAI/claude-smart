/**
 * Read/write ~/.reflexio/.env — preserving unknown keys, comments, and blank
 * lines. Used by the Configure page.
 */

import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import type { ClaudeSmartConfig } from "./types";

const KNOWN_KEYS = [
  "REFLEXIO_URL",
  "REFLEXIO_API_KEY",
  "CLAUDE_SMART_USE_LOCAL_CLI",
  "CLAUDE_SMART_USE_LOCAL_EMBEDDING",
  "CLAUDE_SMART_READ_ONLY",
  "CLAUDE_SMART_CLI_PATH",
  "CLAUDE_SMART_CLI_TIMEOUT",
  "CLAUDE_SMART_STATE_DIR",
] as const;

const KNOWN = new Set<string>(KNOWN_KEYS);

const BOOL_KEYS = new Set([
  "CLAUDE_SMART_USE_LOCAL_CLI",
  "CLAUDE_SMART_USE_LOCAL_EMBEDDING",
  "CLAUDE_SMART_READ_ONLY",
]);

function defaultReflexioUrl(): string {
  return `http://localhost:${process.env.BACKEND_PORT || "8071"}/`;
}

function envPath(): string {
  return path.join(os.homedir(), ".reflexio", ".env");
}

function parseLine(line: string): { key: string; value: string } | null {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith("#")) return null;
  const eq = trimmed.indexOf("=");
  if (eq < 0) return null;
  const key = trimmed.slice(0, eq).trim();
  let value = trimmed.slice(eq + 1).trim();
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    value = value.slice(1, -1);
  }
  return { key, value };
}

export async function readConfig(): Promise<ClaudeSmartConfig> {
  const defaults: ClaudeSmartConfig = {
    REFLEXIO_URL: defaultReflexioUrl(),
    REFLEXIO_API_KEY: "",
    CLAUDE_SMART_USE_LOCAL_CLI: false,
    CLAUDE_SMART_USE_LOCAL_EMBEDDING: false,
    CLAUDE_SMART_READ_ONLY: false,
    CLAUDE_SMART_CLI_PATH: "",
    CLAUDE_SMART_CLI_TIMEOUT: "120",
    CLAUDE_SMART_STATE_DIR: "",
  };
  let text: string;
  try {
    text = await fs.readFile(envPath(), "utf-8");
  } catch {
    return defaults;
  }
  const out: ClaudeSmartConfig = { ...defaults };
  for (const line of text.split("\n")) {
    const pair = parseLine(line);
    if (!pair) continue;
    if (!KNOWN.has(pair.key)) continue;
    if (BOOL_KEYS.has(pair.key)) {
      out[pair.key] = pair.value === "1" || pair.value.toLowerCase() === "true";
    } else {
      out[pair.key] = pair.value;
    }
  }
  return out;
}

export async function writeConfig(update: Partial<ClaudeSmartConfig>): Promise<void> {
  const file = envPath();
  await fs.mkdir(path.dirname(file), { recursive: true });

  const safeUpdate = Object.fromEntries(
    Object.entries(update).filter(([k]) => KNOWN.has(k)),
  );

  let existing = "";
  try {
    existing = await fs.readFile(file, "utf-8");
  } catch {
    existing = "";
  }

  const lines = existing.split("\n");
  const seen = new Set<string>();
  const outLines: string[] = [];

  for (const line of lines) {
    const pair = parseLine(line);
    if (!pair) {
      outLines.push(line);
      continue;
    }
    if (pair.key in safeUpdate) {
      seen.add(pair.key);
      const raw = safeUpdate[pair.key];
      outLines.push(`${pair.key}=${formatValue(pair.key, raw)}`);
    } else {
      outLines.push(line);
    }
  }

  for (const key of Object.keys(safeUpdate)) {
    if (seen.has(key)) continue;
    const raw = safeUpdate[key];
    if (raw === undefined || raw === "") continue;
    outLines.push(`${key}=${formatValue(key, raw)}`);
  }

  const content = outLines.join("\n");
  await fs.writeFile(file, content.endsWith("\n") ? content : content + "\n", {
    encoding: "utf-8",
    mode: 0o600,
  });
}

function formatValue(key: string, raw: unknown): string {
  if (BOOL_KEYS.has(key)) {
    return raw === true || raw === "1" || raw === "true" ? "1" : "0";
  }
  return String(raw ?? "");
}

export { KNOWN_KEYS };
