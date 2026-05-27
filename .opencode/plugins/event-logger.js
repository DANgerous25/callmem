// Passive event logger for OpenCode plugin diagnostics.
//
// Writes every event that OpenCode dispatches into `.opencode/events.log`
// (one JSON line per event, plus a "plugin loaded" line on startup).
// Purpose: discover which events actually fire — and in what order —
// before extending callmem's auto-briefing listener. The known issue is
// `session.created` not always firing for plugins; this log will reveal
// the alternative we should listen on.
//
// Safety: every block is wrapped in try/catch. The plugin never throws
// into OpenCode, never mutates state, never imports anything that could
// fail. Pure diagnostic.

import { appendFile, mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'

const LOG_PATH = '.opencode/events.log'
const MAX_PAYLOAD_CHARS = 4000

async function safeLog(line) {
  try {
    await mkdir(dirname(LOG_PATH), { recursive: true })
    await appendFile(LOG_PATH, line + '\n', 'utf8')
  } catch {
    // diagnostic-only; never bubble
  }
}

/** @type {import('@opencode-ai/plugin').Plugin} */
export default async (_ctx) => {
  await safeLog(JSON.stringify({
    ts: new Date().toISOString(),
    kind: 'plugin.loaded',
  }))

  return {
    event: async ({ event }) => {
      try {
        const ts = new Date().toISOString()
        const type = (event && event.type) || '<unknown>'
        let props
        try {
          props = JSON.stringify(event?.properties ?? {})
        } catch {
          props = '<unserializable>'
        }
        if (props.length > MAX_PAYLOAD_CHARS) {
          props = props.slice(0, MAX_PAYLOAD_CHARS) + '...(truncated)'
        }
        await safeLog(JSON.stringify({ ts, type, props }))
      } catch {
        // never bubble
      }
    },
  }
}
