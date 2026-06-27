import { spawn } from "node:child_process"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { AssistantBuffer } from "./assistant-buffer.js"
import { sessionIDFrom } from "./internal.js"
import { chatMessagePayload, eventPayload, stopPayload, toolAfterPayload } from "./payload.js"

type HookResult = Record<string, unknown>

type PluginInput = {
  directory: string
}

type EventInput = {
  event: {
    type?: string
    properties?: Record<string, unknown>
  }
}

const MODULE_DIR = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = resolve(MODULE_DIR, "../..")
const SCRIPTS_DIR = resolve(PLUGIN_ROOT, "scripts")
const HOOK_ENTRY = resolve(SCRIPTS_DIR, "hook_entry.sh")
const BACKEND_SERVICE = resolve(SCRIPTS_DIR, "backend-service.sh")
const DASHBOARD_SERVICE = resolve(SCRIPTS_DIR, "dashboard-service.sh")

function contextFrom(result: HookResult): string {
  const hookOutput = result.hookSpecificOutput
  if (!hookOutput || typeof hookOutput !== "object") return ""
  const additional = (hookOutput as Record<string, unknown>).additionalContext
  return typeof additional === "string" ? additional : ""
}

function parseFirstJsonObject(text: string): HookResult {
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed.startsWith("{")) continue
    try {
      const parsed = JSON.parse(trimmed)
      if (parsed && typeof parsed === "object") return parsed as HookResult
    } catch {
      continue
    }
  }
  return {}
}

function runScript(script: string, args: string[], payload?: Record<string, unknown>): Promise<HookResult> {
  return new Promise((resolvePromise) => {
    const child = spawn("bash", [script, ...args], {
      cwd: PLUGIN_ROOT,
      env: {
        ...process.env,
        CLAUDE_PLUGIN_ROOT: PLUGIN_ROOT,
        CLAUDE_SMART_HOST: "opencode",
      },
      stdio: ["pipe", "pipe", "pipe"],
    })
    let stdout = ""
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString()
    })
    child.stderr.on("data", (chunk) => {
      process.stderr.write(chunk)
    })
    child.stdin.on("error", () => {
      // Hooks can exit before reading stdin on marker-gated setup failures.
    })
    child.on("error", () => resolvePromise({}))
    child.on("close", () => resolvePromise(parseFirstJsonObject(stdout)))
    try {
      if (payload) child.stdin.write(JSON.stringify(payload))
      child.stdin.end()
    } catch {
      resolvePromise({})
    }
  })
}

function runService(script: string, subcommand: string): Promise<void> {
  return runScript(script, [subcommand]).then(() => undefined)
}

function cacheContext(cache: Map<string, string[]>, sessionID: string, result: HookResult): void {
  const context = contextFrom(result)
  if (!sessionID || !context) return
  const pending = cache.get(sessionID) ?? []
  pending.push(context)
  cache.set(sessionID, pending)
}

async function server(input: PluginInput) {
  const pendingContext = new Map<string, string[]>()
  const activeSessions = new Set<string>()
  const completedAssistantText = new Map<string, string>()
  const assistant = new AssistantBuffer()
  const cwd = input.directory

  async function flushStop(sessionID: string): Promise<void> {
    if (!sessionID || !activeSessions.has(sessionID)) return
    activeSessions.delete(sessionID)
    const text = assistant.text(sessionID) || completedAssistantText.get(sessionID) || ""
    completedAssistantText.delete(sessionID)
    await runScript(
      HOOK_ENTRY,
      ["opencode", "stop"],
      stopPayload({ properties: { sessionID, info: { directory: cwd } } }, cwd, text),
    )
    assistant.clear(sessionID)
  }

  return {
    event: async ({ event }: EventInput) => {
      const type = event.type
      assistant.update(event)
      if (type === "session.created") {
        const payload = eventPayload(event, cwd)
        const sessionID = String(payload.session_id || "")
        if (!sessionID) return
        activeSessions.add(sessionID)
        await runService(BACKEND_SERVICE, "start")
        await runService(DASHBOARD_SERVICE, "start")
        const result = await runScript(HOOK_ENTRY, ["opencode", "session-start"], payload)
        cacheContext(pendingContext, sessionID, result)
        return
      }
      if (type === "session.idle") {
        const payload = eventPayload(event, cwd)
        const sessionID = String(payload.session_id || "")
        if (!sessionID) return
        await flushStop(sessionID)
      }
    },
    "chat.message": async (hookInput: Record<string, unknown>, output: Record<string, unknown>) => {
      const payload = chatMessagePayload(hookInput, output, cwd)
      if (!payload.session_id || !payload.prompt) return
      activeSessions.add(String(payload.session_id || ""))
      const result = await runScript(HOOK_ENTRY, ["opencode", "user-prompt"], payload)
      cacheContext(pendingContext, String(payload.session_id || ""), result)
    },
    "experimental.chat.system.transform": async (hookInput: Record<string, unknown>, output: { system: string[] }) => {
      const sessionID = sessionIDFrom(hookInput)
      const pending = pendingContext.get(sessionID)
      if (!pending?.length) return
      output.system.push(...pending)
      pendingContext.delete(sessionID)
    },
    "tool.execute.after": async (hookInput: Record<string, unknown>, output: Record<string, unknown>) => {
      const payload = toolAfterPayload(hookInput, output, cwd)
      if (!payload.session_id || !payload.tool_name) return
      await runScript(HOOK_ENTRY, ["opencode", "post-tool"], payload)
    },
    "experimental.text.complete": async (
      hookInput: { sessionID?: string },
      output: { text?: string },
    ) => {
      const sessionID = typeof hookInput.sessionID === "string" ? hookInput.sessionID : ""
      if (!sessionID) return
      if (typeof output.text === "string") completedAssistantText.set(sessionID, output.text)
    },
    dispose: async () => {
      await Promise.all([...activeSessions].map((sessionID) => flushStop(sessionID)))
      await runService(DASHBOARD_SERVICE, "session-end")
      await runService(BACKEND_SERVICE, "session-end")
    },
  }
}

export default {
  id: "claude-smart",
  server,
}
