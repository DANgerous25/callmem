# A/B benchmark — does callmem actually save tokens?

The `callmem usage` analytics measure *consumption*: how often a session
calls `mem_*` tools, how many entity IDs it cites, the briefing it fetched.
That tells you whether memory is being *used*. It does **not** tell you
whether using it actually saves tokens, because every session is different
and we never see the counterfactual.

The only honest way to measure savings is a controlled comparison: run
the same task twice — once with callmem enabled, once with it disabled —
and compare the total tokens the session consumed.

This document walks through that comparison end to end. Allow ~20-30
minutes total to run a meaningful pass; each task pair takes ~5 minutes
of human-driven prompting.

---

## What you're measuring

For each task you'll capture:

| metric | source |
|---|---|
| input tokens (model-perceived) | Claude Code's `/cost` or `/usage` command |
| output tokens | same |
| file reads | count of `Read(` tool calls in transcript |
| user-asked clarifying questions | manual count |
| entity citations | `callmem usage` after the session |

The headline is **total tokens**, but the secondary signals (file reads,
clarification questions) help explain *why* a session ran shorter.

---

## Setup

Pick **3-5 tasks** that:

1. Touch code the project has worked on before (so memory has something
   to surface), but aren't fresh in this week's context.
2. Are concrete enough that two parallel runs would produce comparable
   work (e.g. "Add a CLI flag", not "Refactor the codebase").
3. Each take ~3-10 minutes of agent work.

Good examples:
- "Extend the `callmem usage` output to also include sessions outside the
  default window — match the existing `--since` semantics."
- "Add a unit test for the duplicate-resolution path in
  `_resolve_by_drivers`."
- "Update `AGENTS.md` to document the new `callmem dedupe` command."

Bad examples (too open-ended):
- "Improve the codebase."
- "Fix any bugs you see."

Write each task down on paper or in a scratch file. You will use the
**exact same prompt** for both A and B runs.

---

## Procedure

For each task:

### Run A — callmem enabled (baseline)

1. Ensure the project's `.mcp.json` includes the `callmem` server (it
   already does for the four monitored projects).
2. Start a fresh Claude Code session in the project directory:
   ```sh
   cd /path/to/project && claude
   ```
3. Paste the task prompt verbatim. Do not chat first.
4. Let the agent run to completion. Do not redirect or interrupt unless
   it goes catastrophically off track.
5. When done, run `/cost` and record input/output tokens.
6. Note the session start time.
7. After the session ends, run:
   ```sh
   callmem usage -p . --since 1h --per-session
   ```
   Record the row for this session: `tok`, `rd`, `wr`, `brf`, `cite`.

### Run B — callmem disabled

1. Temporarily disable the MCP server. Easiest way:
   ```sh
   mv .mcp.json .mcp.json.disabled
   ```
   (or remove the `callmem` entry from `.mcp.json` if you have other servers)
2. Start a fresh Claude Code session.
3. Paste the **same** task prompt verbatim.
4. Let it run to completion.
5. Record `/cost` tokens.
6. Restore the MCP config:
   ```sh
   mv .mcp.json.disabled .mcp.json
   ```

### Comparing

Compute, per task:

```
delta_tokens   = run_B.total - run_A.total
delta_pct      = delta_tokens / run_B.total * 100
file_reads_A   = ...
file_reads_B   = ...
```

A positive `delta_pct` means callmem saved tokens. Across 3-5 tasks, a
median saving > 0 is meaningful evidence. A saving > 15% is strong.

---

## What to expect (honest)

callmem is most likely to save tokens on tasks where:
- The agent would otherwise re-read large files it has previously
  worked on.
- A relevant decision or failure exists in memory that prevents a wrong
  turn (e.g. "this approach was tried and didn't work because…").
- A previous session's discoveries are directly applicable.

callmem is unlikely to help (and may *cost* tokens) when:
- The task is entirely fresh — memory has nothing to surface.
- The agent doesn't call `mem_*` tools and the briefing isn't injected.
- The briefing eats budget but the agent ignores it (no citations).

If three out of three tasks show `delta_pct <= 0`, then the AGENTS.md
guidance and the SessionStart hook (Phase 2a) aren't producing actual
consumption — the system is write-only and worth reviewing.

---

## Recording the results

After each pair, append a row to `docs/ab-benchmark-results.csv` (create
on first run):

```csv
date,project,task,run_a_tokens,run_b_tokens,delta_pct,run_a_reads,run_b_reads,citations
2026-05-14,llm-mem,"add --since flag to usage",14820,18430,19.6,3,7,5
```

Five rows over a couple of weeks is enough signal to decide whether the
current usage rate is paying off, or whether the next iteration of the
extractor/briefing is warranted.
