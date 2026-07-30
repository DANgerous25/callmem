// Injects the callmem startup briefing directly into a new session's model
// context — no model relay. The previous approach asked the model to run
// `callmem briefing` via the bash tool and repeat its output verbatim, but
// models tended to summarize/paraphrase it instead of showing it, so the
// briefing effectively never reached the user intact.
//
// Mechanism: `experimental.chat.system.transform` (see
// @opencode-ai/plugin's Hooks type) lets a plugin append strings to the
// system prompt sent with each chat completion. The briefing text is
// fetched once (subprocess) and pushed into `output.system` on a session's
// *first* transform call, so it lands in the model's context as ground
// truth rather than something the model has to fetch and choose to relay.
//
// Eligibility is tracked from `session.created` (see EventSessionCreated in
// @opencode-ai/sdk's types.gen.d.ts): `properties.info.id` is the session
// id — NOT `properties.id` (the bug in the previous version of this file)
// and NOT a flat `properties.sessionID` (that shape only applies to other
// session.* events, e.g. session.idle/session.error). Sub-sessions carry
// `properties.info.parentID` and are skipped so child/task sessions never
// get their own briefing injected.
//
// Known issue: session.created may not fire reliably for plugins (OpenCode
// #14808). If the briefing doesn't appear automatically, /briefing remains
// available as a manual fallback.

import { execFile } from "node:child_process"

const BRIEFING_TIMEOUT_MS = 10_000

function runBriefing(cwd) {
  return new Promise((resolve) => {
    execFile(
      "callmem",
      ["briefing"],
      { cwd, timeout: BRIEFING_TIMEOUT_MS, killSignal: "SIGKILL", maxBuffer: 10 * 1024 * 1024 },
      (error, stdout) => {
        if (error || !stdout || !stdout.trim()) {
          resolve(null)
          return
        }
        resolve(stdout)
      },
    )
  })
}

/** @type {import('@opencode-ai/plugin').Plugin} */
export default async ({ client, directory }) => {
  // sessionID -> Promise<string|null>, for top-level sessions created this
  // process lifetime and not yet injected.
  const pending = new Map()
  // sessionIDs that have already been injected (or definitively failed) —
  // guards against double injection across turns and process-local resumes.
  const injected = new Set()

  async function logQuiet(message, extra) {
    try {
      await client.app.log({
        body: { service: "callmem-briefing", level: "info", message, extra },
      })
    } catch {
      // logging is best-effort; never let it throw
    }
  }

  return {
    event: async ({ event }) => {
      if (event.type !== "session.created") return
      const info = event.properties?.info
      if (!info?.id || info.parentID) return // malformed event, or a sub-session
      if (pending.has(info.id) || injected.has(info.id)) return
      pending.set(info.id, runBriefing(directory).catch(() => null))
    },

    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = input.sessionID
      if (!sessionID || injected.has(sessionID)) return
      const briefingPromise = pending.get(sessionID)
      if (!briefingPromise) return // not a tracked new top-level session

      // Mark injected before awaiting so a concurrent call for the same
      // session can't race past this guard and inject twice.
      injected.add(sessionID)
      pending.delete(sessionID)

      let text = null
      try {
        text = await briefingPromise
      } catch {
        text = null
      }

      if (!text) {
        await logQuiet("callmem briefing unavailable, skipping injection", { sessionID })
        return
      }

      output.system.push(
        "# callmem startup briefing\n\n" +
          text +
          "\n\nPresent the above briefing to the user near the start of your first reply " +
          "(verbatim, preserving formatting), then address their request.",
      )
    },
  }
}
