// Injects the callmem startup briefing directly into a new session's model
// context — no model relay. The previous approach asked the model to run
// `callmem briefing` via the bash tool and repeat its output verbatim, but
// models tended to summarize/paraphrase it instead of showing it, so the
// briefing effectively never reached the user intact.
//
// Mechanism: `experimental.chat.system.transform` (see
// @opencode-ai/plugin's Hooks type) lets a plugin append strings to the
// system prompt sent with each chat completion. This hook is EXPERIMENTAL —
// its name and shape carry no stability guarantee from OpenCode, and if a
// future version removes/renames it, `hooks["experimental.chat.system..."]`
// is simply never called, so the plugin degrades silently to no injection
// (no crash) rather than failing loudly.
//
// The hook fires on every chat completion for a session (system prompt is
// rebuilt per turn), not just the first. The briefing subprocess itself
// still runs exactly once per session (single-flight, cached by sessionID),
// but the RESOLVED TEXT is cached and re-pushed into `output.system` on
// every turn for that session — otherwise the briefing would only appear in
// the first LLM request and vanish from context on turn 2 onward. Caching
// the resolved string (rather than recomputing it) also keeps the injected
// system-prompt content byte-identical turn over turn, which matters for
// providers that prompt-cache on a stable system-prompt prefix.
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

// `callmem briefing` prints this (and exits 0) when the project has no
// `.callmem/memory.db` yet — it is CLI *output*, not a failure, so it isn't
// caught by the error/empty-stdout check below. Treat it as "no briefing
// available" rather than injecting the sentinel text as if it were the
// briefing (which is what "present verbatim" would otherwise do).
const NO_DATABASE_SENTINEL = "No callmem database found"

function runBriefing(cwd) {
  return new Promise((resolve) => {
    execFile(
      "callmem",
      ["briefing"],
      { cwd, timeout: BRIEFING_TIMEOUT_MS, killSignal: "SIGKILL", maxBuffer: 10 * 1024 * 1024 },
      (error, stdout) => {
        const text = stdout ? stdout.trim() : ""
        if (error || !text || text.startsWith(NO_DATABASE_SENTINEL)) {
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
  // sessionID -> Promise<string|null>, single-flight per top-level session
  // created this process lifetime. Resolves once and is awaited (not
  // re-run) on every subsequent transform call for that session.
  const briefings = new Map()
  // sessionIDs for which the "briefing unavailable" line has already been
  // logged — logs exactly once per session, not once per turn.
  const loggedFailure = new Set()

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
      if (briefings.has(info.id)) return
      briefings.set(info.id, runBriefing(directory).catch(() => null))
    },

    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = input.sessionID
      if (!sessionID) return
      const briefingPromise = briefings.get(sessionID)
      if (!briefingPromise) return // not a tracked new top-level session

      let text = null
      try {
        text = await briefingPromise
      } catch {
        text = null
      }

      if (!text) {
        if (!loggedFailure.has(sessionID)) {
          loggedFailure.add(sessionID)
          await logQuiet("callmem briefing unavailable, skipping injection", { sessionID })
        }
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
