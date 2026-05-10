/**
 * Read/write Claude Code project settings used to inject hook-side env vars.
 *
 * This is intentionally separate from ~/.reflexio/.env: reflexio reads that
 * file in the backend process, while claude-smart hooks read env from the
 * Claude Code process tree.
 */

import fs from "node:fs/promises";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { ClaudeCodeHookConfig } from "./types";

const ENV_KEY = "CLAUDE_SMART_ENABLE_OPTIMIZER";

function settingsPath(): string {
  return path.join(workspaceRoot(), ".claude", "settings.local.json");
}

function userSettingsPath(): string {
  return path.join(os.homedir(), ".claude", "settings.json");
}

function workspaceRoot(): string {
  const configured = process.env.CLAUDE_SMART_DASHBOARD_WORKSPACE;
  const start = configured
    ? path.resolve(configured)
    : path.resolve(process.cwd(), "../..");
  return findProjectRoot(start);
}

function findProjectRoot(start: string): string {
  let current = start;
  while (true) {
    if (
      existsSync(path.join(current, ".claude")) ||
      existsSync(path.join(current, ".git"))
    ) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) return start;
    current = parent;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}

function parseEnabled(value: unknown): boolean {
  if (value === undefined || value === null || value === "") return true;
  if (value === false || value === 0) return false;
  if (typeof value === "string" && value.trim() === "0") return false;
  return true;
}

function optimizerValue(settings: Record<string, unknown>): boolean | null {
  const env = asRecord(settings.env);
  if (!(ENV_KEY in env)) return null;
  return parseEnabled(env[ENV_KEY]);
}

async function readSettingsFile(file: string): Promise<Record<string, unknown>> {
  try {
    const text = await fs.readFile(file, "utf-8");
    return asRecord(JSON.parse(text));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

export async function readClaudeCodeHookConfig(): Promise<ClaudeCodeHookConfig> {
  const localPath = settingsPath();
  const globalPath = userSettingsPath();
  const [localSettings, userSettings] = await Promise.all([
    readSettingsFile(localPath),
    readSettingsFile(globalPath),
  ]);
  const localValue = optimizerValue(localSettings);
  const userValue = optimizerValue(userSettings);
  const effectiveValue = localValue ?? userValue ?? true;
  return {
    CLAUDE_SMART_ENABLE_OPTIMIZER: effectiveValue,
    effectiveValue,
    localValue,
    userValue,
    settingsPath: localPath,
    userSettingsPath: globalPath,
  };
}

export async function writeClaudeCodeHookConfig(
  update: Partial<ClaudeCodeHookConfig>,
): Promise<void> {
  const file = settingsPath();
  const settings = await readSettingsFile(file);
  const env = asRecord(settings.env);

  if (!("CLAUDE_SMART_ENABLE_OPTIMIZER" in update)) return;
  env[ENV_KEY] = update.CLAUDE_SMART_ENABLE_OPTIMIZER ? "1" : "0";

  settings.env = env;
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, `${JSON.stringify(settings, null, 2)}\n`, {
    encoding: "utf-8",
    mode: 0o600,
  });
}
