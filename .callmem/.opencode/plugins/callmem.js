// callmem — push-based event capture for OpenCode.
// Hooks into lifecycle events and POSTs to the callmem daemon's HTTP ingest endpoint.
// Replaces polling-based DB adapter with realtime capture.

/** @type {import('@opencode-ai/plugin').Plugin} */
export const CallmemPlugin = async ({ project, client }) => {
  const port = 9097  // overridden per-project via callmem.toml [ui].port
  const baseUrl = `http://127.0.0.1:${port}/api`
  let sessionId = null
  let pendingEvents = []

  async function flush() {
    if (pendingEvents.length === 0) return
    const events = pendingEvents
    pendingEvents = []
    try {
      await fetch(`${baseUrl}/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ events, session_id: sessionId }),
      })
    } catch (e) {
      // daemon may be down — drop silently, polling adapter is fallback
    }
  }

  async function startSession(agentName) {
    try {
      const res = await fetch(`${baseUrl}/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          events: [],
          session_action: "start",
          agent_name: agentName || "opencode",
        }),
      })
      const data = await res.json()
      sessionId = data.session_id
    } catch (e) {
      // daemon down — events will be captured by DB polling adapter
    }
  }

  async function endSession() {
    await flush()
    if (!sessionId) return
    try {
      await fetch(`${baseUrl}/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          events: [],
          session_id: sessionId,
          session_action: "end",
        }),
      })
    } catch (e) {
      // best-effort
    }
    sessionId = null
  }

  function enqueue(type, content, metadata) {
    if (!content) return
    pendingEvents.push({
      type,
      content: typeof content === "string" ? content : JSON.stringify(content),
      timestamp: new Date().toISOString(),
      metadata: metadata || undefined,
    })
    if (pendingEvents.length >= 10) flush()
  }

  return {
    event: async ({ event }) => {
      switch (event.type) {
        case "session.created": {
          await startSession("opencode")
          break
        }
        case "session.idle":
        case "session.error": {
          await endSession()
          break
        }
        case "message.part.updated": {
          const part = event.properties || event.data || {}
          const partType = part.type

          if (partType === "text") {
            const role = part.role || (part.path?.role) || "assistant"
            const text = part.text || part.content || ""
            if (text) {
              enqueue(role === "user" ? "prompt" : "response", text)
            }
          } else if (partType === "tool") {
            const toolName = part.tool || "unknown"
            const args = part.state?.input || part.args || ""
            const argsStr = typeof args === "object"
              ? JSON.stringify(args).slice(0, 200)
              : String(args).slice(0, 200)
            enqueue("tool_call", `${toolName}(${argsStr})`)
          } else if (partType === "patch") {
            const files = part.files || []
            for (const f of files) {
              enqueue("file_change", `modified: ${f}`)
            }
          }
          break
        }
        case "file.edited": {
          const path = event.properties?.path || event.data?.path || "unknown"
          enqueue("file_change", `modified: ${path}`)
          break
        }
        case "todo.updated": {
          const todo = event.properties || event.data || {}
          if (todo.content) {
            enqueue("todo", todo.content, { status: todo.status })
          }
          break
        }
      }
    },
  }
}
