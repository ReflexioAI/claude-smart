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
  PlaybookApplicationStat,
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

type RawInjectedEntry = CitedItem & {
  dashboard_url?: string;
  rule_url?: string;
  ts?: number;
};

export interface RuleResolution {
  id: string;
  href: string;
  title: string;
  kind: CitedItem["kind"];
}

interface AppliedRulesOptions {
  daysBack?: number;
  limit?: number;
}

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

async function readInjectedJsonl(filePath: string): Promise<RawInjectedEntry[]> {
  const text = await fs.readFile(filePath, "utf-8");
  const out: RawInjectedEntry[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const rec = JSON.parse(trimmed);
      if (
        rec &&
        typeof rec === "object" &&
        typeof rec.id === "string" &&
        rec.id.length > 0
      ) {
        out.push(rec);
      }
    } catch {
      // skip malformed line, matches state.py behaviour
    }
  }
  return out;
}

function hrefForInjectedEntry(entry: RawInjectedEntry): string | null {
  const realId = entry.real_id;
  if (typeof realId === "string" && realId.length > 0) {
    if (entry.kind === "profile") {
      return `/preferences/project/${encodeURIComponent(realId)}`;
    }
    if (entry.kind === "playbook") {
      const scope = entry.source_kind === "agent_playbook" ? "shared" : "project";
      return `/skills/${scope}/${encodeURIComponent(realId)}`;
    }
  }
  if (typeof entry.dashboard_url === "string" && entry.dashboard_url.length > 0) {
    try {
      const parsed = new URL(entry.dashboard_url);
      return `${parsed.pathname}${parsed.search}`;
    } catch {
      if (entry.dashboard_url.startsWith("/")) return entry.dashboard_url;
    }
  }
  return null;
}

function canonicalHrefForCitedItem(item: CitedItem): string | null {
  const realId = item.real_id;
  if (typeof realId !== "string" || realId.length === 0) return null;
  if (item.kind === "profile") {
    return `/preferences/project/${encodeURIComponent(realId)}`;
  }
  const scope = item.source_kind === "agent_playbook" ? "shared" : "project";
  return `/skills/${scope}/${encodeURIComponent(realId)}`;
}

function ruleHrefForCitedItem(item: CitedItem): string | null {
  if (/^[ps]\d+(?:-[A-Za-z0-9]{1,8})?$/.test(item.id)) {
    return `/rules/${encodeURIComponent(item.id)}`;
  }
  return canonicalHrefForCitedItem(item);
}

function statKeyForCitedItem(item: CitedItem): string {
  const realId = item.real_id && item.real_id.length > 0 ? item.real_id : item.id;
  const sourceKind =
    item.kind === "profile" ? "profile" : item.source_kind ?? "user_playbook";
  return `${item.kind}:${sourceKind}:${realId}`;
}

export async function resolveRuleLink(
  citationId: string,
): Promise<RuleResolution | null> {
  if (!/^[ps]\d+(?:-[A-Za-z0-9]{1,8})?$/.test(citationId)) return null;
  const dir = stateDir();
  let entries: string[];
  try {
    entries = await fs.readdir(dir);
  } catch {
    return null;
  }
  const files = (
    await Promise.all(
      entries
        .filter((entry) => entry.endsWith(".injected.jsonl"))
        .map(async (entry) => {
          const fullPath = path.join(dir, entry);
          const stat = await fs.stat(fullPath).catch(() => null);
          return stat?.isFile() ? { fullPath, mtimeMs: stat.mtimeMs } : null;
        }),
    )
  )
    .filter((entry): entry is { fullPath: string; mtimeMs: number } => !!entry)
    .sort((a, b) => b.mtimeMs - a.mtimeMs);

  for (const file of files) {
    const records = await readInjectedJsonl(file.fullPath).catch(() => []);
    for (let i = records.length - 1; i >= 0; i--) {
      const entry = records[i];
      if (entry.id !== citationId) continue;
      const href = hrefForInjectedEntry(entry);
      if (!href) continue;
      return {
        id: entry.id,
        href,
        title: entry.title || entry.id,
        kind: entry.kind,
      };
    }
  }
  return null;
}

export async function listAppliedRules(
  opts: AppliedRulesOptions = {},
): Promise<PlaybookApplicationStat[]> {
  const daysBack = opts.daysBack ?? 30;
  const limit = opts.limit ?? 20;
  const cutoff =
    daysBack > 0 ? Math.floor(Date.now() / 1000) - daysBack * 24 * 60 * 60 : null;
  const dir = stateDir();
  let entries: string[];
  try {
    entries = await fs.readdir(dir);
  } catch {
    return [];
  }

  const stats = new Map<string, PlaybookApplicationStat>();
  for (const entry of entries) {
    if (!entry.endsWith(".jsonl") || entry.endsWith(".injected.jsonl")) continue;
    const fullPath = path.join(dir, entry);
    const records = await readJsonl(fullPath).catch(() => []);
    for (let idx = 0; idx < records.length; idx++) {
      const rec = records[idx];
      if (
        rec.role !== "Assistant" ||
        !rec.cited_items ||
        rec.cited_items.length === 0
      ) {
        continue;
      }
      const ts = typeof rec.ts === "number" ? rec.ts : null;
      if (cutoff !== null && ts !== null && ts < cutoff) continue;

      for (const item of rec.cited_items) {
        const realId =
          item.real_id && item.real_id.length > 0 ? item.real_id : item.id;
        const key = statKeyForCitedItem(item);
        const prev = stats.get(key);
        const href = canonicalHrefForCitedItem(item) ?? ruleHrefForCitedItem(item);
        if (prev) {
          prev.applied_count += 1;
          if ((ts ?? 0) >= (prev.last_applied_at ?? 0)) {
            prev.citation_id = item.id;
            prev.title = item.title || prev.title;
            prev.href = href ?? prev.href;
            prev.last_applied_at = ts;
            prev.last_interaction_id = idx;
          }
          continue;
        }
        stats.set(key, {
          real_id: realId,
          citation_id: item.id,
          kind: item.kind,
          source_kind:
            item.kind === "profile"
              ? "profile"
              : item.source_kind ?? "user_playbook",
          title: item.title || item.id,
          href: href ?? undefined,
          applied_count: 1,
          last_applied_at: ts,
          last_interaction_id: idx,
        });
      }
    }
  }

  return Array.from(stats.values())
    .sort((a, b) => {
      if (b.applied_count !== a.applied_count) {
        return b.applied_count - a.applied_count;
      }
      return (b.last_applied_at ?? 0) - (a.last_applied_at ?? 0);
    })
    .slice(0, limit);
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
  const dir = stateDir();
  const files = [
    path.join(dir, `${sessionId}.jsonl`),
    path.join(dir, `${sessionId}.injected.jsonl`),
  ];
  let removedAny = false;
  for (const file of files) {
    try {
      await fs.unlink(file);
      removedAny = true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") return false;
    }
  }
  return removedAny;
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
