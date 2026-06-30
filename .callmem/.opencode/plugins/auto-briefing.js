// Auto-displays callmem briefing when an OpenCode session starts.
// Listens for session.created and injects a prompt that runs `callmem briefing`
// (the live CLI). SESSION_SUMMARY.md is deprecated — never read it; the DB is the source of truth.
//
// Known issue: session.created may not fire reliably for plugins (OpenCode #14808).
// If the briefing doesn't appear automatically, use /briefing as a fallback.

/** @type {import('@opencode-ai/plugin').Plugin} */
export default async (ctx) => {
  let triggered = false

  return {
    event: async ({ event }) => {
      if (event.type === 'session.created' && !triggered) {
        triggered = true
        try {
          await ctx.client.session.prompt({
            path: { id: event.properties.id },
            body: {
              parts: [
                {
                  type: 'text',
                  text: 'Run the bash command `callmem briefing` and present its output verbatim (preserve the box-drawing). Then greet the user, briefly highlight any open TODOs or unresolved failures from the briefing, and ask what they would like to work on.',
                },
              ],
            },
          })
        } catch (e) {
          // session.created may not fire reliably (known issue)
          // User can always type /briefing manually
        }
      }
    },
  }
}
