/**
 * Server-side reader for claude-smart JSONL session buffers.
 * Mirrors the format documented in src/claude_smart/state.py:
 *   - {role: "User" | "Assistant" | "Assistant_tool", ...}
 *   - {published_up_to: N}  watermark
 */

import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import type {
  CitedItem,
  SessionDetail,
  SessionSummary,
  SessionTurn,
  ToolUsed,
  UserActionType,
} from "./types";

// Mirrors _TOOL_DATA_FIELD_MAX_LEN in plugin/src/claude_smart/state.py — we
// truncate to the same length the publisher ships to reflexio so the
// dashboard renders the exact bytes the extractor sees.
const TOOL_DATA_FIELD_MAX_LEN = 256;

function truncateToolField<T>(value: T): T {
  if (typeof value === "string" && value.length > TOOL_DATA_FIELD_MAX_LEN) {
    return value.slice(0, TOOL_DATA_FIELD_MAX_LEN) as T;
  }
  return value;
}

export function stateDir(): string {
  const override = process.env.CLAUDE_SMART_STATE_DIR;
  if (override) return override;
  return path.join(os.homedir(), ".claude-smart", "sessions");
}

type RawRecord = {
  role?: "User" | "Assistant" | "Assistant_tool";
  content?: string;
  ts?: number;
  user_id?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_output?: string;
  status?: string;
  user_action?: UserActionType;
  user_action_description?: string;
  cited_items?: CitedItem[];
  published_up_to?: number;
};

async function readJsonl(filePath: string): Promise<RawRecord[]> {
  const text = await fs.readFile(filePath, "utf-8");
  const out: RawRecord[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      out.push(JSON.parse(trimmed));
    } catch {
      // skip malformed line, matches state.py behaviour
    }
  }
  return out;
}

function foldTurns(records: RawRecord[]): {
  turns: SessionTurn[];
  publishedUpTo: number;
  learningInteractionCount: number;
  lastTs: number | null;
  firstTs: number | null;
  preview: string | null;
} {
  let published = 0;
  let pendingTools: ToolUsed[] = [];
  const turns: SessionTurn[] = [];
  let learningInteractionCount = 0;
  let lastTs: number | null = null;
  let firstTs: number | null = null;
  let preview: string | null = null;

  for (let idx = 0; idx < records.length; idx++) {
    const rec = records[idx];
    if (typeof rec.published_up_to === "number") {
      published = rec.published_up_to;
      pendingTools = [];
      continue;
    }
    const role = rec.role;
    if (role === "Assistant_tool") {
      const entry: ToolUsed = {
        tool_name: rec.tool_name ?? "",
        status: rec.status ?? "success",
      };
      const toolData: { input?: Record<string, unknown>; output?: string } = {};
      if (rec.tool_input && Object.keys(rec.tool_input).length > 0) {
        const input: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(rec.tool_input)) {
          input[k] = truncateToolField(v);
        }
        toolData.input = input;
      }
      if (typeof rec.tool_output === "string" && rec.tool_output.length > 0) {
        toolData.output = truncateToolField(rec.tool_output);
      }
      if (toolData.input || toolData.output) {
        entry.tool_data = toolData;
      }
      pendingTools.push(entry);
      continue;
    }
    if (role !== "User" && role !== "Assistant") continue;

    if (
      role === "Assistant" &&
      rec.cited_items &&
      rec.cited_items.length > 0
    ) {
      learningInteractionCount += 1;
    }
    if (typeof rec.ts === "number") {
      lastTs = rec.ts;
      if (firstTs === null) firstTs = rec.ts;
    }

    const turn: SessionTurn = {
      role,
      content: rec.content ?? "",
      ts: rec.ts,
      user_id: rec.user_id,
      user_action: rec.user_action,
      user_action_description: rec.user_action_description,
    };
    if (role === "Assistant" && pendingTools.length) {
      turn.tools_used = pendingTools;
      pendingTools = [];
    }
    if (role === "Assistant" && rec.cited_items && rec.cited_items.length) {
      turn.cited_items = rec.cited_items;
    }
    if (
      preview === null &&
      role === "User" &&
      typeof turn.content === "string" &&
      turn.content.trim()
    ) {
      preview = turn.content.trim().slice(0, 240);
    }
    turns.push(turn);
  }

  return {
    turns,
    publishedUpTo: published,
    learningInteractionCount,
    lastTs,
    firstTs,
    preview,
  };
}

export async function listSessions(): Promise<SessionSummary[]> {
  const dir = stateDir();
  let entries: string[];
  try {
    entries = await fs.readdir(dir);
  } catch {
    return [];
  }

  const summaries: SessionSummary[] = [];
  for (const entry of entries) {
    if (!entry.endsWith(".jsonl")) continue;
    if (entry.endsWith(".injected.jsonl")) continue;
    const fullPath = path.join(dir, entry);
    const records = await readJsonl(fullPath).catch(() => []);
    const {
      turns,
      publishedUpTo,
      learningInteractionCount,
      lastTs,
      firstTs,
      preview,
    } = foldTurns(records);
    summaries.push({
      session_id: entry.replace(/\.jsonl$/, ""),
      turn_count: turns.length,
      learning_interaction_count: learningInteractionCount,
      last_activity: lastTs,
      first_activity: firstTs,
      published_up_to: publishedUpTo,
      preview,
      source: "local",
    });
  }
  summaries.sort((a, b) => (b.last_activity ?? 0) - (a.last_activity ?? 0));
  return summaries;
}

export async function deleteSession(sessionId: string): Promise<boolean> {
  const file = path.join(stateDir(), `${sessionId}.jsonl`);
  try {
    await fs.unlink(file);
    return true;
  } catch {
    return false;
  }
}

export async function deleteAllSessions(): Promise<number> {
  const dir = stateDir();
  let entries: string[];
  try {
    entries = await fs.readdir(dir);
  } catch {
    return 0;
  }
  let count = 0;
  for (const entry of entries) {
    if (!entry.endsWith(".jsonl")) continue;
    try {
      await fs.unlink(path.join(dir, entry));
      count += 1;
    } catch {
      // ignore
    }
  }
  return count;
}

export async function readSession(
  sessionId: string,
): Promise<SessionDetail | null> {
  const file = path.join(stateDir(), `${sessionId}.jsonl`);
  let records: RawRecord[];
  try {
    records = await readJsonl(file);
  } catch {
    return null;
  }
  const { turns, publishedUpTo } = foldTurns(records);
  return { session_id: sessionId, turns, published_up_to: publishedUpTo };
}
