import { eventProperties, type EventLike, isRecord, sessionIDFrom } from "./internal.js"

type PartLike = {
  type?: string
  text?: string
}

export type PythonPayload = Record<string, unknown>

function textFromParts(parts: unknown): string {
  if (!Array.isArray(parts)) return ""
  return parts
    .map((part: PartLike) => (part && part.type === "text" && typeof part.text === "string" ? part.text : ""))
    .filter(Boolean)
    .join("\n\n")
}

export function eventPayload(event: EventLike, cwd: string): PythonPayload {
  const properties = eventProperties(event)
  const info = isRecord(properties.info) ? properties.info : {}
  return {
    session_id: sessionIDFrom(properties) || sessionIDFrom(info),
    cwd: typeof info.directory === "string" ? info.directory : cwd,
  }
}

export function chatMessagePayload(input: Record<string, unknown>, output: Record<string, unknown>, cwd: string): PythonPayload {
  const message = isRecord(output.message) ? output.message : {}
  const prompt =
    textFromParts(output.parts) ||
    (typeof message.content === "string" ? message.content : "") ||
    textFromParts(message.parts)
  return {
    session_id: sessionIDFrom(input),
    cwd,
    prompt,
  }
}

export function normalizeToolName(tool: string): string {
  const lowered = tool.toLowerCase()
  if (lowered === "edit") return "Edit"
  if (lowered === "write") return "Write"
  if (lowered === "apply_patch") return "apply_patch"
  if (["bash", "shell", "terminal", "exec", "command"].includes(lowered)) return "Bash"
  return tool
}

export function normalizeToolInput(tool: string, args: unknown): Record<string, unknown> {
  if (!isRecord(args)) return {}
  const out: Record<string, unknown> = { ...args }
  const copy = (from: string, to: string) => {
    if (from in args && !(to in out)) out[to] = args[from]
  }
  copy("filePath", "file_path")
  copy("oldString", "old_string")
  copy("newString", "new_string")
  copy("patchText", "command")
  if (normalizeToolName(tool) === "Bash") {
    copy("cmd", "command")
    copy("script", "command")
  }
  return out
}

export function toolAfterPayload(input: Record<string, unknown>, output: Record<string, unknown>, cwd: string): PythonPayload {
  const tool = typeof input.tool === "string" ? input.tool : ""
  const text = typeof output.output === "string" ? output.output : ""
  const response: Record<string, unknown> = {
    output: text,
    stdout: text,
  }
  if (typeof output.title === "string") response.title = output.title
  if (isRecord(output.metadata)) response.metadata = output.metadata
  if (isRecord(output.metadata) && output.metadata.error) response.error = output.metadata.error
  return {
    session_id: sessionIDFrom(input),
    cwd,
    tool_name: normalizeToolName(tool),
    tool_input: normalizeToolInput(tool, input.args),
    tool_response: response,
  }
}

export function stopPayload(event: EventLike, cwd: string, lastAssistantMessage: string): PythonPayload {
  return {
    ...eventPayload(event, cwd),
    last_assistant_message: lastAssistantMessage,
  }
}
