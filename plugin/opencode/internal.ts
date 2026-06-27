export type EventLike = {
  type?: string
  properties?: Record<string, unknown>
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

export function eventProperties(event: EventLike): Record<string, unknown> {
  return isRecord(event.properties) ? event.properties : (event as Record<string, unknown>)
}

export function sessionIDFrom(value: unknown): string {
  if (!isRecord(value)) return ""
  const raw = value.sessionID ?? value.session_id
  return typeof raw === "string" ? raw : ""
}
