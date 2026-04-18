// Auto-displays llm-mem briefing when an OpenCode session starts.
// Listens for session.created and injects a prompt to present SESSION_SUMMARY.md.
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
                  text: 'Read SESSION_SUMMARY.md and present the startup briefing. Greet the user, state the project name, summarize the most recent session activity, highlight any open TODOs or unresolved failures, and ask what they would like to work on.',
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
