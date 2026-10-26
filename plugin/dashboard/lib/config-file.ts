/**
 * Read/write ~/.claude-smart/.env — the env file claude-smart's hooks and
 * backend read — preserving unknown keys, comments, and blank
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
  "CLAUDE_SMART_READ_ONLY",
  "CLAUDE_SMART_CLI_PATH",
  "CLAUDE_SMART_LEARNING_MODEL",
  "CLAUDE_SMART_CLI_TIMEOUT",
  "CLAUDE_SMART_STATE_DIR",
] as const;

const KNOWN = new Set<string>(KNOWN_KEYS);

const BOOL_KEYS = new Set([
  "CLAUDE_SMART_USE_LOCAL_CLI",
  "CLAUDE_SMART_READ_ONLY",
]);

function defaultReflexioUrl(): string {
  return `http://localhost:${process.env.BACKEND_PORT || "8071"}/`;
}

function envPath(): string {
  return path.join(os.homedir(), ".claude-smart", ".env");
}

export function parseLine(line: string): { key: string; value: string } | null {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith("#")) return null;
  const eq = trimmed.indexOf("=");
  if (eq < 0) return null;
  // `export KEY=value` is valid in this file; _lib.sh and env_config.py
  // strip the prefix too.
  const key = trimmed
    .slice(0, eq)
    .trim()
    .replace(/^export\s+/, "");
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
    // Absent means the local CLI default (smart-install seeds it as 1).
    // Reading false would make the next save disable authenticated CLI use.
    CLAUDE_SMART_USE_LOCAL_CLI: true,
    CLAUDE_SMART_READ_ONLY: false,
    CLAUDE_SMART_CLI_PATH: "",
    CLAUDE_SMART_LEARNING_MODEL: "",
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

/**
 * The Reflexio URL and key the hooks resolve: a key present in the env file
 * wins (even when empty), otherwise this process's environment. The file is
 * read on every call, so a save on the Configure page takes effect without a
 * restart instead of losing to the values this process inherited at launch.
 */
export async function managedReflexioSettings(): Promise<{
  url: string;
  apiKey: string;
}> {
  const values = new Map<string, string>();
  try {
    for (const line of (await fs.readFile(envPath(), "utf-8")).split("\n")) {
      const pair = parseLine(line);
      if (pair) values.set(pair.key, pair.value);
    }
  } catch {
    // No file: fall back to the environment below.
  }
  const pick = (key: string): string =>
    values.has(key) ? (values.get(key) ?? "") : (process.env[key] ?? "");
  return {
    url: deriveFromBackendPort(pick("REFLEXIO_URL")),
    apiKey: pick("REFLEXIO_API_KEY"),
  };
}

/**
 * Mirror of claude_smart_derive_reflexio_url_from_backend_port (_lib.sh): the
 * 8071 spellings on localhost/127.0.0.1 mean the bundled backend, which runs
 * on BACKEND_PORT. The hooks rewrite them the same way. (An empty URL stays
 * empty: the proxy then uses its own default and sends no key.)
 */
export function deriveFromBackendPort(url: string): string {
  const defaults = new Set([
    "http://localhost:8071",
    "http://localhost:8071/",
    "http://127.0.0.1:8071",
    "http://127.0.0.1:8071/",
  ]);
  return defaults.has(url) ? defaultReflexioUrl() : url;
}

export async function writeConfig(
  update: Partial<ClaudeSmartConfig>,
): Promise<void> {
  const model = update.CLAUDE_SMART_LEARNING_MODEL;
  if (
    model &&
    (typeof model !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model))
  )
    throw new Error("Learning model must be a valid model ID");
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

  // Clearing the API key means local mode, but the hooks and service scripts
  // pick the mode from the URL: a remote URL left without a key would keep
  // them remote with no credentials. Drop that URL so the result is local.
  const current = new Map<string, string>();
  for (const line of lines) {
    const pair = parseLine(line);
    if (pair) current.set(pair.key, pair.value);
  }
  const currentUrl = current.get("REFLEXIO_URL") || defaultReflexioUrl();
  const requestedUrl = String(safeUpdate.REFLEXIO_URL || currentUrl);
  const origin = (value: string) => {
    try {
      return new URL(value).origin;
    } catch {
      return "";
    }
  };
  if (
    (origin(requestedUrl) !== origin(currentUrl) ||
      !current.get("REFLEXIO_URL")) &&
    !("REFLEXIO_API_KEY" in safeUpdate)
  ) {
    const prefix = isRemoteReflexioUrl(requestedUrl) ? "MANAGED" : "LOCAL";
    const rememberedUrl = current.get(`CLAUDE_SMART_${prefix}_URL`) || "";
    const rememberedKey =
      origin(rememberedUrl) === origin(requestedUrl)
        ? current.get(`CLAUDE_SMART_${prefix}_API_KEY`) || ""
        : "";
    if (prefix === "MANAGED" && !rememberedKey)
      throw new Error(
        "Changing managed endpoints requires an API key for that endpoint",
      );
    safeUpdate.REFLEXIO_API_KEY = rememberedKey;
  }
  const finalValue = (key: string): string =>
    key in safeUpdate
      ? String(safeUpdate[key] ?? "")
      : (current.get(key) ?? "");
  const dropUrl =
    !finalValue("REFLEXIO_API_KEY").trim() &&
    isRemoteReflexioUrl(finalValue("REFLEXIO_URL"));
  if (dropUrl) {
    delete safeUpdate.REFLEXIO_URL;
  }
  const remember = (url: string, apiKey: string) => {
    if (!url) return;
    const prefix = isRemoteReflexioUrl(url) ? "MANAGED" : "LOCAL";
    safeUpdate[`CLAUDE_SMART_${prefix}_URL`] = url;
    safeUpdate[`CLAUDE_SMART_${prefix}_API_KEY`] = apiKey;
  };
  remember(
    current.get("REFLEXIO_URL") || "",
    current.get("REFLEXIO_API_KEY") || "",
  );
  remember(
    dropUrl ? defaultReflexioUrl() : finalValue("REFLEXIO_URL"),
    finalValue("REFLEXIO_API_KEY"),
  );
  if (
    "REFLEXIO_API_KEY" in update &&
    !String(update.REFLEXIO_API_KEY || "").trim() &&
    isRemoteReflexioUrl(currentUrl)
  ) {
    safeUpdate.CLAUDE_SMART_MANAGED_API_KEY = "";
  }

  for (const line of lines) {
    const pair = parseLine(line);
    if (dropUrl && pair?.key === "REFLEXIO_URL") continue;
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
  try {
    await fs.chmod(file, 0o600);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  await fs.writeFile(file, content.endsWith("\n") ? content : content + "\n", {
    encoding: "utf-8",
    mode: 0o600,
  });
}

/** Mirror of claude_smart_reflexio_url_is_remote in plugin/scripts/_lib.sh. */
export function isRemoteReflexioUrl(url: string): boolean {
  if (!url) return false;
  return !/^http:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(\/?|:.*)$/.test(
    url,
  );
}

function formatValue(key: string, raw: unknown): string {
  if (BOOL_KEYS.has(key)) {
    return raw === true || raw === "1" || raw === "true" ? "1" : "0";
  }
  return String(raw ?? "");
}

export { KNOWN_KEYS };
