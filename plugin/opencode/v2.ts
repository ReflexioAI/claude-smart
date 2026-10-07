// OpenCode V2 bridge. V2 rejects `{ id, server }` and does not call V1 hook
// keys. `setup` reuses the V1 factory and registers the V2 equivalents.
// Fail-soft: capture must never break a turn.
// experimental.text.complete has no V2 hook; session.next.text.ended carries
// the completed assistant text.

type V1Hooks = {
  event?: (input: { event: Record<string, unknown> }) => Promise<void>
  dispose?: () => Promise<void>
  "chat.message"?: (input: Record<string, unknown>, output: Record<string, unknown>) => Promise<void>
  "experimental.chat.system.transform"?: (
    input: Record<string, unknown>,
    output: { system: string[] },
  ) => Promise<void>
  "tool.execute.after"?: (input: Record<string, unknown>, output: Record<string, unknown>) => Promise<void>
  "experimental.text.complete"?: (input: { sessionID?: string }, output: { text?: string }) => Promise<void>
}

type ServerFactory = (input: { directory: string }) => Promise<V1Hooks>

type SystemPart = { type?: string; text?: string } | string

type V2Context = {
  location?: { directory?: string }
  sessionsOutliveDispose?: boolean
  session?: { hook: (name: string, fn: (event: any) => Promise<void> | void) => Promise<{ dispose?: () => void }> }
  tool?: { hook: (name: string, fn: (event: any) => Promise<void> | void) => Promise<{ dispose?: () => void }> }
  event?: { subscribe: (options: { signal?: AbortSignal }) => AsyncIterable<Record<string, unknown>> }
}

function textOf(value: unknown): string {
  if (typeof value === "string") return value
  if (!value || typeof value !== "object") return ""
  const record = value as Record<string, unknown>
  if (typeof record.text === "string") return record.text
  if (typeof record.output === "string") return record.output
  if (typeof record.content === "string") return record.content
  if (Array.isArray(record.content)) return record.content.map(textOf).filter(Boolean).join("\n")
  return ""
}

async function safeRegister(label: string, fn: () => Promise<void>): Promise<void> {
  try {
    await fn()
  } catch (err) {
    const message = err instanceof Error ? err.message : err
    console.warn(`[claude-smart] hook skipped (${label}):`, message)
  }
}

export function createSetup(server: ServerFactory) {
  return async function setup(ctx: V2Context = {}): Promise<(() => Promise<void>) | void> {
    const directory = ctx.location?.directory || process.cwd()
    let v1: V1Hooks
    try {
      v1 = await server({ directory })
    } catch (err) {
      const message = err instanceof Error ? err.message : err
      console.warn("[claude-smart] disabled — OpenCode factory failed:", message)
      return
    }
    if (!v1) return

    const disposers: Array<() => Promise<void> | void> = []
    const onDispose = (maybe: { dispose?: () => void } | void) => {
      if (maybe && typeof maybe.dispose === "function") disposers.push(() => maybe.dispose!())
    }

    if (v1["chat.message"] && ctx.session && typeof ctx.session.hook === "function") {
      await safeRegister("prompt", async () => {
        onDispose(await ctx.session!.hook("prompt", async (event) => {
          try {
            const text = event && event.prompt ? event.prompt.text : ""
            const sessionID = event && event.sessionID
            if (typeof text !== "string" || !text.length || !sessionID) return
            await v1["chat.message"]!(
              { sessionID },
              { parts: [{ type: "text", text }] },
            )
          } catch {
            // capture must never break admission
          }
        }))
      })
    }

    if (v1["experimental.chat.system.transform"] && ctx.session && typeof ctx.session.hook === "function") {
      await safeRegister("context", async () => {
        onDispose(await ctx.session!.hook("context", async (event) => {
          try {
            const system: SystemPart[] = Array.isArray(event.system) ? event.system : []
            const before = system.map((part) => (typeof part === "string" ? part : part && part.text || ""))
            // V1 mutates the array it is handed. Hand it a copy, then append
            // additions at the END.
            //
            // Cache compatibility: opencode's Anthropic Messages provider adds
            // cache breakpoints on system[0] and system[last] and treats the
            // parts between them as one cached block. The injected rules are
            // relevance-gated and change between turns, so inserting them at
            // index 1 would shift the cached prefix and throw away prompt-cache
            // hits on the stable system body. Appending keeps the whole stable
            // prefix byte-identical across turns; only the small rules tail is
            // re-sent. This matches the V1 native code (output.system.push).
            const output = { system: before.slice() }
            await v1["experimental.chat.system.transform"]!({ sessionID: event.sessionID }, output)
            if (!Array.isArray(output.system)) return
            const added = output.system.filter((item) => !before.includes(item))
            if (added.length) {
              event.system.push(...added.map((text: string) => ({ type: "text", text })))
            }
          } catch {
            // never break the turn
          }
        }))
      })
    }

    if (v1["tool.execute.after"] && ctx.tool && typeof ctx.tool.hook === "function") {
      await safeRegister("execute.after", async () => {
        onDispose(await ctx.tool!.hook("execute.after", async (event) => {
          try {
            if (!event || !event.sessionID || !event.tool) return
            const result = event.status === "completed" ? event.result || {} : {}
            await v1["tool.execute.after"]!(
              { tool: event.tool, sessionID: event.sessionID, args: event.input },
              {
                output: event.status === "error" ? textOf(event.error) : textOf(result),
                title: typeof result.title === "string" ? result.title : undefined,
                metadata: result.metadata,
              },
            )
          } catch {
            // capture must never break the call
          }
        }))
      })
    }

    if (v1.event && ctx.event && typeof ctx.event.subscribe === "function") {
      const controller = new AbortController()
      void (async () => {
        try {
          for await (const event of ctx.event!.subscribe({ signal: controller.signal })) {
            if (controller.signal.aborted) break
            try {
              const record = event as { type?: string; properties?: Record<string, unknown> }
              if (record.type === "session.next.text.ended" && v1["experimental.text.complete"]) {
                const props = record.properties || {}
                if (typeof props.sessionID === "string" && typeof props.text === "string") {
                  await v1["experimental.text.complete"]({ sessionID: props.sessionID }, { text: props.text })
                }
              }
              await v1.event!({ event: event as Record<string, unknown> })
            } catch {
              // capture must never break the session
            }
          }
        } catch (err) {
          if (!controller.signal.aborted) {
            const message = err instanceof Error ? err.message : err
            console.warn("[claude-smart] event subscription failed:", message)
          }
        }
      })()
      disposers.push(() => controller.abort())
    }

    return async () => {
      for (const dispose of disposers) {
        try { await dispose() } catch { /* unload must not throw */ }
      }
      if (ctx.sessionsOutliveDispose) return
      try {
        if (typeof v1.dispose === "function") await v1.dispose()
      } catch {
        // unload must not throw
      }
    }
  }
}
