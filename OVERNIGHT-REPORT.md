# Overnight callmem improvements — morning report

**Run completed:** 2026-05-13, late evening.
**Scope:** Phases 0–5 of the plan. All four projects covered.

---

## TL;DR

| | |
|---|---|
| **Done autonomously** | Phase 0 (cleanup + factual footer), Phase 1 (usage CLI), Phase 2b (AGENTS.md guidance), Phase 3 (tighter extractor + retroactive dedup), Phase 4 (A/B benchmark doc), Phase 5 (rollout + restart). |
| **Blocked, needs your hand** | Phase 2a (SessionStart hook). Auto-mode classifier refused to modify `~/.claude/settings.json` and `.claude/settings.local.json` as agent-config self-modification. Paste-ready JSON is in §"One thing left for you" below. |
| **Risk taken** | DBs backed up to `*.pre-overnight-20260513-222542` in each `.callmem/` before any destructive ops. `callmem resolve` and `callmem dedupe` mark losers stale rather than deleting — fully reversible. |
| **Commits** | 5 local commits across 4 repos. Nothing pushed. |

---

## What changed, phase by phase

### Phase 0 — Quick wins

**Briefing footer is honest now.**
Before:
```
587,320t captured │ 3,311t to read │ 99.7% saved
```
After:
```
briefing: 2,000t │ 100 entities surfaced │ 597,551t captured (full history in Web UI)
```
The 99.7% was compression ratio against work the agent would never have read anyway — not real savings. Now the footer states facts only.

**`callmem resolve` swept all four projects.** Net closed: 174 TODOs/failures (boat-ess: 34, ellma: 25, llm-mem: 83, screen-lizard: 2). These were items the extractor had captured as open but later sessions had implicitly resolved without keyword-matching well enough for the inline auto-close.

### Phase 1 — Measurement (the load-bearing piece)

New `callmem usage` command. Per-project usage table that aggregates from the existing events table — no schema changes.

```sh
callmem usage --all -p /home/dan/llm-mem        # all 4 projects, last 30d
callmem usage -p . --since 7d --per-session     # detail rows
callmem usage --json                            # machine-readable
```

**Current baseline (30d, post-cleanup):**

| project | sessions | mem used | briefing fetched | citations |
|---|---:|---:|---:|---:|
| boat-ess | 192 | 13 (7%) | 0 (0%) | 101 |
| ellma-trading-bot | 175 | 20 (11%) | 7 (4%) | 140 |
| llm-mem | 224 | 24 (11%) | 3 (1%) | 186 |
| screen-lizard | 13 | 1 (8%) | 1 (8%) | 0 |

"Citations" is the strong signal: response text containing `#XXXXXXXX` short-IDs that resolve to a real entity in the DB. The regex catches candidates; the resolver filters out session-id references and doc placeholders like `#XXXXXXXX` in this very file.

The honest read of this table: usage is at ~8-11% across the active projects. The Phase 2 changes are aimed at moving that up.

### Phase 2 — Make agents actually read memory

**2b. Updated AGENTS.md** in all four projects with two new sections:

- *"Consult memory before disk"* — agent should call `mem_get_briefing` at the start of non-trivial tasks; `mem_file_context` before re-reading touched files; `mem_search` before asking the user.
- *"Cite entity IDs when memory informs your work"* — explicit `#XXXXXXXX` format with examples and a "do not invent IDs" rule.

This is what makes the citation metric generatable. Until now there was no instruction telling agents to leave a paper trail.

**2a. SessionStart hook — blocked, needs you.** See "One thing left for you" below.

### Phase 3 — Quality fixes

**3a. Extraction prompt tightened.** Changes:

- Now receives `{prior_titles}` — a list of entities already extracted in the same session, so the LLM can avoid emitting near-duplicates.
- Dedup rule moved to the top and given an explicit failure example (the footer issue capture pattern we observed in this session).
- Concrete anchors (file:line, function, command) required when present.
- Synopsis downgraded to "at most one sentence, omit if redundant" — was the main source of corporate-prose bloat.

**3b. New `callmem dedupe` command.** Finds near-duplicate entities by normalised-title similarity (0.82 default), bucketed by prefix for speed. Guards:
- Same `(project, type)` only — a `discovery` and a `failure` about the same thing stay separate.
- Same digit tokens in title required — prevents merging "migration to v3" with "v4".
- Marks losers stale with `superseded_by → survivor`; no deletes.

Ran live on all four projects:

| project | duplicates merged |
|---|---:|
| boat-ess | 33 |
| ellma-trading-bot | 19 |
| llm-mem | 88 |
| screen-lizard | 1 |
| **total** | **141** |

These were genuine reformulations of the same observation (multiple ULIDs for "Fix event text truncation", three rows for "Implement UI files view", etc.). The 88 in llm-mem aligns with the diagnosis from yesterday's analysis.

### Phase 4 — A/B benchmark

`docs/ab-benchmark.md` — the procedure for actually measuring tokens saved. I deliberately did **not** fabricate a result.

To run it yourself, ~20-30 minutes total:
1. Pick 3-5 concrete tasks (the doc has examples and anti-examples).
2. For each: run twice from a fresh Claude Code session — once with `.mcp.json` intact, once with `callmem` removed from MCP servers.
3. Record `/cost` tokens each time.
4. Append to `docs/ab-benchmark-results.csv`.

This is the only honest way to claim "X% tokens saved." Everything before this point measures *consumption*, not *savings*.

### Phase 5 — Rollout

- `uv tool install --force --reinstall /home/dan/llm-mem` → global binary now at 0.3.2 with all changes.
- All four `callmem-*.service` units restarted; all `active`; all UIs returning HTTP 200.
- Per-project AGENTS.md committed in each repo's `main` branch.

**Nothing was pushed.** Decided that was beyond the autonomous remit — push when you're ready.

---

## One thing left for you

The SessionStart hook needs your hand. The auto-mode classifier is correctly treating modifications to `~/.claude/settings.json` or any `.claude/settings.local.json` as agent-config self-modification, which the overnight authorisation didn't specifically cover. Reasonable safety rail; I won't try to bypass it.

**To install the hook** (this makes the briefing inject itself at the start of every session where `.callmem/` exists):

Edit `~/.claude/settings.json` and add the `hooks` block:

```json
{
  "permissions": { "defaultMode": "auto" },
  "skipAutoPermissionPrompt": true,
  "enabledPlugins": {
    "frontend-design@claude-plugins-official": true
  },
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "test -d .callmem && /home/dan/.local/bin/callmem briefing 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

The `test -d .callmem &&` guard means it's silent for projects without callmem set up. Reversible — just delete the `hooks` block. After saving, the next session you start in any of the four projects should print a briefing as the first system message.

---

## Verifying tomorrow

Quick sanity check commands:

```sh
# All daemons still up
callmem status --all

# Honest usage view, last 24h after a few sessions
callmem usage --all -p /home/dan/llm-mem --since 1d --per-session

# Dedupe inspection (post-cleanup state)
callmem dedupe -p . --dry-run --limit 5    # should report no clusters

# Generate a briefing and see the new factual footer
callmem briefing -p . | tail -5
```

If usage stays at ~10% over the next few days even with AGENTS.md guidance in place, the SessionStart hook is the next lever to pull. If the hook is in and citations don't climb, the AGENTS.md citation guidance isn't biting hard enough and the next iteration is to lower the citation friction (e.g. surface the most-likely-relevant entity in every briefing's first line).

---

## Safety net

If anything in this run feels wrong, restore is one command per project:

```sh
cp .callmem/memory.db.pre-overnight-20260513-222542 .callmem/memory.db
systemctl --user restart callmem-<project>.service
```

The backups are at every project's `.callmem/memory.db.pre-overnight-20260513-222542`. Sizes: 12-18 MB each. Safe to delete after a few days of confidence.

---

## Commits

| repo | commit | summary |
|---|---|---|
| llm-mem | `b6eae05` | feat(briefing): web UI URL as final line + PyPI update check |
| llm-mem | `72d55e4` | feat(callmem): usage analytics, dedup, tighter extraction, factual footer |
| boat-ess | `3d2e2e9` | docs(agents): consult memory before disk; cite entity IDs |
| ellma-trading-bot | `6c19c64` | docs(agents): consult memory before disk; cite entity IDs |
| screen-lizard | `4e9322f` | docs(agents): consult memory before disk; cite entity IDs |

All on `main`. None pushed.
