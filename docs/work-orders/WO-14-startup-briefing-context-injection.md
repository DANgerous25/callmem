# WO-14: Startup Briefing Upgrade — Context Economics, Visual Formatting, Auto-Injection

## Objective

Upgrade the startup briefing to match claude-mem's quality: rich formatted output with context economics, colour-coded observation index, token accounting, and automatic injection into OpenCode's context (no manual MCP call needed).

## Reference

The claude-mem startup briefing (pasted by user) includes:
1. **Header**: `[project-name] recent context, <datetime>`
2. **Legend**: emoji + colour-coded category key
3. **Column Key**: explains "Read" and "Work" token columns
4. **Context Index explanation**: what this briefing is, how to use it, trust guidance
5. **Context Economics block**: observations loaded, tokens to read, work investment, savings percentage
6. **Chronological observation timeline**: grouped by date, sessions as headers, observations with file associations, emoji category icons, IDs, timestamps
7. **Session summary at bottom**: structured INVESTIGATED/LEARNED/COMPLETED/NEXT STEPS
8. **Footer**: total token savings line + link to web viewer

---

## 1. Context Economics

### What to compute

The briefing generator must calculate and display:

| Metric | How to compute |
|--------|---------------|
| **Observations loaded** | Count of entities included in briefing |
| **Tokens to read** | Sum of `token_count` for all entities in the briefing (estimate if null: `len(content) / 4`) |
| **Work investment** | Sum of `token_count` for ALL events in the project (total raw tokens captured) |
| **Savings percentage** | `1 - (briefing_tokens / work_investment) * 100` |

### Display format (in briefing text)

```
Context Economics
  Loading: 42 observations (12,350 tokens to read)
  Work investment: 385,000 tokens spent on research, building, and decisions
  Your savings: 97% reduction from reuse
```

### Files to modify
- `src/callmem/core/briefing.py` — add `_compute_economics()` method, include in `Briefing` dataclass
- `src/callmem/core/repository.py` — add query method for total project event token count

---

## 2. Visual Briefing Format

### Current format
Plain markdown with `### Active TODOs`, `### Recent Decisions`, etc. Functional but not visually scannable.

### New format
A rich text briefing designed for terminal display with category icons:

```
[callmem] recent context, 2026-04-17 1:30am HKT
────────────────────────────────────────────────

Legend: 🟢 feature | 🔴 bugfix | 🔵 discovery | ⚖️ decision | 📋 todo | ❌ failure | 🔬 research | 🔄 change | 📝 fact

Context Economics
  Loading: 42 observations (12,350 tokens to read)
  Work investment: 385,000 tokens of captured work
  Your savings: 97% reduction from reuse

Apr 17, 2026

#S3 Rewrote OpenCode import to use SQLite DB (Apr 17, 12:43 AM)
  src/callmem/adapters/opencode_import.py
    #E45  12:43 AM  🟢  OpenCode import adapter rewritten for SQLite
    #E46            🔵  OpenCode stores sessions in SQLite, not JSON files
  scripts/setup.py
    #E47  12:43 AM  🔄  Setup wizard updated for SQLite-based session import

#S4 Added card-based memory feed UI (Apr 17, 12:58 AM)
  src/callmem/ui/routes/feed.py
    #E48  12:58 AM  🟢  Memory feed route with real-time htmx polling
  src/callmem/ui/templates/feed_partial.html
    #E49            🟢  Card layout with category badges and timestamps

Latest Session Summary:
  Built card-based memory feed UI inspired by claude-mem

  ✅ COMPLETED
  - Feed route at / with entity + session timeline
  - htmx polling every 3s for real-time updates
  - Category badges with colour coding
  
  📋 NEXT STEPS
  - Add expandable cards with Key Points / Synopsis
  - Improve extraction categories

Access 385k tokens of past work for just 12,350t.
View observations live @ http://trilogy:9090
```

### Implementation

Update `BriefingGenerator.generate()` to produce this format:

1. **Header + separator**: project name, current datetime in user's timezone
2. **Legend**: emoji mapping for each entity type
3. **Economics block**: computed stats
4. **Observation timeline**: 
   - Group entities by date, then by session
   - For each entity: show `#E{id_prefix}`, time, category emoji, title
   - For sessions with file-level entity associations (if metadata has file info): group by file
   - Otherwise just list entities under session header
5. **Latest session summary**: from most recent ended session
6. **Footer**: savings line + web viewer URL

### Entity-to-emoji mapping

```python
CATEGORY_EMOJI = {
    "feature": "🟢",
    "bugfix": "🔴",
    "discovery": "🔵",
    "decision": "⚖️",
    "todo": "📋",
    "failure": "❌",
    "research": "🔬",
    "change": "🔄",
    "fact": "📝",
}
```

### Briefing dataclass updates

```python
@dataclass
class Briefing:
    project_name: str
    content: str
    token_count: int
    components: dict[str, int]
    generated_at: str
    # New fields:
    observations_loaded: int = 0
    read_tokens: int = 0
    work_investment: int = 0
    savings_pct: float = 0.0
```

### Files to modify
- `src/callmem/core/briefing.py` — complete rewrite of format generation
- `src/callmem/core/prompts.py` — no change (already has BRIEFING_COMPRESSION_PROMPT)

---

## 3. Auto-Injection into OpenCode

### Problem
Currently, OpenCode only gets context if it decides to call the `get_briefing` MCP tool. claude-mem uses hooks to inject automatically. We need the same.

### Solution: SESSION_SUMMARY.md auto-generation

OpenCode reads `AGENTS.md` and project-level markdown files at session start. We can auto-generate a `SESSION_SUMMARY.md` file in the project root that gets picked up automatically.

### How it works

1. After generating a briefing, write it to `{project_worktree}/SESSION_SUMMARY.md`
2. The file is overwritten on every session start and periodically during the session
3. OpenCode reads this file automatically as part of its context loading
4. This is fire-and-forget — no MCP call needed

### Implementation

Add a `write_session_summary()` method to `BriefingGenerator`:

```python
def write_session_summary(
    self,
    project_id: str,
    project_name: str,
    worktree_path: str,
    max_tokens: int | None = None,
) -> Briefing:
    """Generate briefing and write to SESSION_SUMMARY.md in project root."""
    briefing = self.generate(project_id, project_name, max_tokens)
    summary_path = Path(worktree_path) / "SESSION_SUMMARY.md"
    summary_path.write_text(briefing.content, encoding="utf-8")
    return briefing
```

### When to write
- On session start event (adapter receives session.created)
- After each extraction batch completes (worker callback)
- On session end event

### Configuration

In `config.toml`:
```toml
[briefing]
auto_write_session_summary = true
session_summary_filename = "SESSION_SUMMARY.md"
```

### .gitignore
The setup wizard should offer to add `SESSION_SUMMARY.md` to the project's `.gitignore` (it's ephemeral, not source code).

### Files to modify
- `src/callmem/core/briefing.py` — add `write_session_summary()`
- `src/callmem/models/config.py` — add `auto_write_session_summary` and `session_summary_filename` config fields
- `src/callmem/core/workers.py` — call `write_session_summary()` after extraction batch
- `src/callmem/adapters/opencode.py` — call on session start/end events
- `scripts/setup.py` — offer to add SESSION_SUMMARY.md to .gitignore

---

## 4. Briefing in Web UI

### Add a preview to the Briefing page

The existing `/briefing` page should show the formatted briefing as it would appear in the terminal — a live preview of what OpenCode sees.

### Implementation
- Render the briefing content in a `<pre>` block with monospace font to preserve the terminal-like formatting
- Show the economics stats as a separate styled block above the pre
- Add a "Copy to clipboard" button

### Files to modify
- `src/callmem/ui/routes/briefing.py` — pass economics data + formatted content
- `src/callmem/ui/templates/briefing.html` — styled preview with copy button

---

## Acceptance Criteria

1. [ ] Briefing includes Context Economics: observations loaded, read tokens, work investment, savings %
2. [ ] Briefing uses emoji-coded legend for entity categories
3. [ ] Briefing shows chronological observation timeline grouped by date and session
4. [ ] Each observation shows ID prefix, time, category emoji, and title
5. [ ] Latest session summary included with structured sections
6. [ ] Footer shows total savings and web viewer URL
7. [ ] `Briefing` dataclass includes `observations_loaded`, `read_tokens`, `work_investment`, `savings_pct`
8. [ ] `SESSION_SUMMARY.md` auto-written to project worktree on session start and after extraction batches
9. [ ] Config has `auto_write_session_summary` toggle (default: true)
10. [ ] Setup wizard offers to add SESSION_SUMMARY.md to .gitignore
11. [ ] Web UI briefing page shows formatted preview with economics stats
12. [ ] Copy-to-clipboard button on briefing page
13. [ ] All existing tests pass, new tests for economics computation and file writing
14. [ ] `make lint` clean, `make test` all pass
15. [ ] Committed and pushed
