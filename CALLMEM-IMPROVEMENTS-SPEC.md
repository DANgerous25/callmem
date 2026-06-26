# Callmem Improvement Spec — 2026-06-26

Derived from a full-day build session of ModelMaestro (a sibling project that
uses callmem as its context store).  These are real problems observed during
active development, not hypothetical issues.

## Status: project_overview feature already in progress — skip section 1 if done.

---

## 1. Project Overview Entity (PRIORITY: HIGH)

A single always-visible summary at the top of every briefing that describes
what the project IS, its current status, and next steps.

### Requirements

- `project_overview` table: one row per project (`project_id`, `content`,
  `updated_at`, `updated_by`).  Upsert on write.
- `mem_set_overview(content="...")` MCP tool — primary API for agents.
- `callmem overview set` (stdin/`--file`) and `callmem overview show` CLI.
- Briefing shows overview FIRST, before "Context Economics", truncated to
  ~500 tokens with "..." if longer.  Silent skip if no overview set.
- `callmem re-extract` must NOT touch the overview table.
- Optional: auto-generate overview from extracted entities on first
  extraction if none is set manually.

---

## 2. Extraction Health Visibility (PRIORITY: HIGH)

### Problem

During the ModelMaestro build session, 43 events were captured but 0
entities were extracted because the LLM backend was misconfigured
(wrong endpoint, wrong env var name).  callmem reported "No prior
memories found" as if the project were new — no warning that extraction
was failing.  This went unnoticed for hours.

### Requirements

**`callmem status` output should include extraction health:**

```
  Events:       43
  Entities:     0  ⚠️  (43 events unextracted — check LLM backend)
  Last extraction: never
```

If `events > 0` and `entities == 0`, show a warning.  If `events > entities
* 2` (rough heuristic), show a warning.

**`callmem briefing` should warn when extraction has failed:**

Instead of "No prior memories found. This is a new project." (which is
misleading when there are 43 events), show:

```
  ⚠️ 43 events captured but 0 entities extracted.
     LLM backend may be unreachable. Run `callmem doctor` to diagnose.
```

**`callmem doctor` should test the LLM backend:**

```
  LLM backend:
    Backend: openai_compat
    Endpoint: https://openrouter.ai/api/v1
    Model: z-ai/glm-4.7-flash
    API key: ✓ set (OPENROUTER_KEY)
    Reachable: ✓ (test request succeeded)
```

If the backend is unreachable, show the error and suggest fixes.

**Background daemon should log extraction failures** to a visible location
(e.g. `.callmem/extraction.log`) so users can diagnose without digging
through systemd journal.

---

## 3. Setup Wizard — Smart Key Detection (PRIORITY: HIGH)

### Problem

`callmem setup` configured `open.bigmodel.cn` as the endpoint and
`LLM_MEM_API_KEY` as the env var — neither was valid on this machine.
The user's actual key was `OPENROUTER_KEY` pointing at OpenRouter.

### Requirements

**Scan environment for known API keys during setup:**

Check these env vars in order and suggest the first one found:
- `OPENROUTER_KEY` / `OPENROUTER_API_KEY` → OpenRouter
- `OPENAI_API_KEY` → OpenAI direct
- `ANTHROPIC_API_KEY` → Anthropic direct
- `DEEPSEEK_API_KEY` → DeepSeek
- `CALLMEM_API_KEY` / `LLM_MEM_API_KEY` → user-provided generic

**Test the endpoint before writing config:**

After the user selects a backend, send a real test request
(`{"messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}`)
and verify a 200 response.  If it fails, show the error and let the user
retry or choose a different backend.

**Suggest a cheap model for extraction:**

For OpenRouter, suggest `z-ai/glm-4.7-flash` or similar cheap model
(under $0.50/M tokens).  Extraction doesn't need a frontier model.

**Never write untested config:**

If the test request fails, do NOT write the config.  Show the error and
offer to retry with different settings.

---

## 4. Agent Instructions — Concrete Decision Rules (PRIORITY: MEDIUM)

### Problem

The AGENTS.md template tells agents to use callmem but doesn't teach them
*when* or *why*.  Instructions like "consult memory before disk" are too
vague — agents don't know the specific decision points.

### Requirements

Update the AGENTS.md template (in `callmem/templates/`) with concrete rules:

```markdown
## Memory Decision Rules

- **Session start**: Call `mem_get_briefing` once. This is your context.
- **Before reading a file you've worked on**: Call `mem_file_context(path)`
  first. If the timeline covers your task, skip the raw read (saves ~95%
  tokens).
- **Before asking the user a clarifying question**: Call `mem_search(query)`
  — the answer may already be stored.
- **When you make a design decision**: Call `mem_ingest` with type
  `decision` immediately. Don't wait for the session to end.
- **When something surprising happens**: Call `mem_ingest` with type
  `discovery` or `failure`.
- **If briefing shows 0 entities but N events**: Run `callmem re-extract`
  — extraction may have failed.
- **Before re-reading a file**: Always check `mem_file_context` first.
  Only read from disk if the memory is stale or missing.
- **Long sessions (50+ messages)**: Call `mem_check_context` every ~30
  messages. Compress if recommended.
```

---

## 5. compile_context Usage (PRIORITY: MEDIUM)

### Problem

`mem_compile_context` is the token-saving killer feature — it compresses
project history into a model-specific context string.  But nobody uses it:
- ModelMaestro's orchestrator calls it optionally and often skips it
- No opencode agent calls it before reading files
- The AGENTS.md doesn't mention it

### Requirements

**Advertise compile_context in the briefing:**

When there's substantial project history (>10 entities), add a hint at the
bottom of the briefing:

```
  💡 Tip: Use mem_compile_context(target_model="your-model") to get
     compressed project context (~500 tokens) instead of re-reading
     files (~5000 tokens).
```

**Track and report token savings:**

When `mem_compile_context` is called, estimate the tokens saved vs reading
all source files.  Show cumulative savings in `callmem status`:

```
  Context savings: ~47K tokens (~$0.12) via compile_context (12 calls)
```

---

## 6. Token Economics Visibility (PRIORITY: LOW)

### Problem

callmem burns LLM tokens for extraction.  There's no visibility into
whether the extraction cost is worth the context savings.

### Requirements

**`callmem status` should show cost summary:**

```
  Economics:
    Extraction cost: ~$0.03 (43 events processed)
    Context savings: ~$0.50 (12 file reads skipped, 47K tokens saved)
    Net savings:     ~$0.47
```

This requires tracking:
- Number of extraction LLM calls and estimated cost
- Number of `compile_context` / `mem_file_context` calls and estimated
  tokens saved (vs reading raw files)

**Extraction batching:**

Don't extract every single event immediately.  Batch events (e.g. every
5 events or every 5 minutes, whichever comes first) to reduce LLM calls.

---

## 7. Auto-Ingestion — Don't Rely on Agents Remembering (PRIORITY: HIGH)

### Problem

During the ModelMaestro build session, significant insights (token
economics analysis, extraction failure diagnosis, architecture decisions)
were discussed but NOT ingested to callmem until the user explicitly
asked "did you add that to callmem?" at the end.  The agent forgot or
didn't think to ingest mid-conversation.  This means context is lost if
the user doesn't prompt, and the burden of memory management falls on
the user instead of the system.

### Root Cause

Ingestion is entirely manual.  The agent must remember to call
`mem_ingest` with the right type and content at the right time.
AGENTS.md says to do it, but agents don't consistently follow
instructions under cognitive load (complex coding tasks).

### Requirements

**7a. Immediate extraction on explicit ingest**

When an agent calls `mem_ingest`, callmem should immediately attempt
entity extraction on that event (not wait for the next batch extraction
cycle).  This ensures explicitly ingested events become searchable
entities without delay.

**7b. Auto-detect ingestable content in the SSE adapter**

The opencode SSE adapter sees every prompt and response.  It should
detect patterns that indicate ingestable content and auto-ingest:

- **Decisions**: "let's go with X", "we'll use Y approach", "decision: Z"
- **Discoveries**: "the issue was", "turns out", "I found that"
- **Failures**: error traces, "this didn't work because"
- **TODOs**: "we need to", "next step is", "still need to"

Implementation: a lightweight classifier (regex patterns + optional LLM
verification) that runs on every assistant response.  When a pattern
matches, auto-ingest with the detected type.  The agent doesn't need to
do anything.

This is the most impactful change — it means callmem captures insights
automatically without relying on agent compliance.

**7c. Session-end memory checkpoint**

When a session ends (or the daemon detects no activity for N minutes),
run a final LLM pass over the session's events to catch anything that
wasn't auto-detected:

```
Review this session's events.  Were any decisions, discoveries, or
failures NOT captured as entities?  Extract them now.
```

**7d. Nudge the agent at natural breakpoints**

When the agent completes a task or makes a significant statement, the
MCP server could return a hint in the next tool response:

```
  💡 You just made a design decision.  Call mem_ingest(type="decision",
     content="...") to persist it.
```

This is optional and should be subtle — don't nag the agent on every
message, only on significant breakpoints.

**7e. Don't duplicate auto-ingested content**

If the agent explicitly ingests something that was already auto-ingested,
deduplicate by content similarity (same as the existing dedupe logic).

1. Project overview (already in progress)
2. Extraction health visibility (section 2) — prevents silent failures
3. Setup wizard smart key detection (section 3) — prevents misconfiguration
4. Agent instructions (section 4) — teaches agents when to use callmem
5. compile_context usage (section 5) — unlocks token savings
6. Token economics (section 6) — visibility into ROI
7. Auto-ingestion (section 7) — don't rely on agents remembering
