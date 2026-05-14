# Session Briefing — Run Once at Start of Each Session

At the start of every new OpenCode session, your **very first reply** must:

1. Run the bash command `callmem briefing` (no arguments)
2. Present its output verbatim — preserve the box-drawing, emoji, and spacing
3. After the briefing output, briefly greet the user and address their actual request

**Run this exactly once per session.** Check your prior replies in this session's history: if one already contains a `callmem briefing` output block (look for the box-drawing header starting with `╭───`), do **not** run it again — just address the user's current request normally.

If the `callmem` CLI is unavailable on PATH, fall back to the `mem_get_briefing` MCP tool.

**Do not** read `SESSION_SUMMARY.md` — that file is deprecated and may be stale or missing. The CLI / MCP tool is the live source of truth, generated fresh from `.callmem/memory.db` on each invocation.

Why this matters: the briefing surfaces recent decisions, open TODOs, unresolved failures, and the most recent session's activity. Loading it once at the start of a session is far cheaper than rediscovering the same context by re-reading files mid-conversation.
