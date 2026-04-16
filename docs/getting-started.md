# Getting Started — Your First Session with OpenCode + llm-mem

This is a concrete, step-by-step walkthrough for getting llm-mem running on your machine and executing your first work order with OpenCode/GLM.

## Prerequisites

You need:
- **Python 3.11+** with `uv` (recommended) or `pip`
- **Ollama** running locally with a model pulled (e.g. `qwen3:8b`)
- **OpenCode** installed and configured with your GLM API key
- **Git** configured with push access to `DANgerous25/llm-mem`

## Step 1 — Clone and install

```bash
git clone https://github.com/DANgerous25/llm-mem.git
cd llm-mem

# Install with uv (preferred)
uv sync --extra dev

# Or pip
pip install -e ".[dev]"
```

Verify the install:

```bash
# Run the test suite — all 19 tests should pass
pytest tests/ -v

# Check the CLI loads
python -m llm_mem.cli --help
```

## Step 2 — Pull an Ollama model

llm-mem uses a local Ollama model for memory maintenance (summarization, entity extraction, sensitive data classification). This is separate from your interactive coding model (GLM).

```bash
# Pull the recommended model
ollama pull qwen3:8b

# Verify it's available
ollama list
```

If you have a GPU with enough VRAM, `qwen3:30b` gives better extraction quality. Either works.

## Step 3 — Read the bootstrap memory

Before writing any code, read the project memory. This is what OpenCode/GLM should do at the start of every session:

```bash
# Display the bootstrap memory files
python scripts/session_load.py
```

This prints:
- **SESSION.md** — What happened in the last session
- **DECISIONS.md** — All design decisions made so far (009 and counting)
- **TODO.md** — Current work order progress

Read these yourself too — they're the canonical source of project state.

## Step 4 — Configure OpenCode MCP (for later)

The MCP server isn't functional yet (that's WO-05), but you can pre-configure it so OpenCode knows about it:

In your `opencode.json`:

```json
{
  "mcp": {
    "llm-mem": {
      "type": "local",
      "command": ["python", "-m", "llm_mem.mcp.server"],
      "enabled": false
    }
  }
}
```

Set `"enabled": false` for now. Flip it to `true` once WO-05 is complete and the server actually runs.

## Step 5 — Start your first work order

Open your terminal, `cd` into the llm-mem directory, and start OpenCode:

```bash
cd ~/llm-mem
opencode
```

### The prompt to give OpenCode/GLM

Copy-paste this as your first message to the agent:

```
Read AGENTS.md completely. Then read .llm-mem/SESSION.md, .llm-mem/DECISIONS.md, and .llm-mem/TODO.md.

Then open docs/work-orders/WO-01-project-setup.md and verify all acceptance criteria are met.
If anything is missing, implement it. Run pytest tests/ -v to confirm all tests pass.

When done, commit with: git commit -m "chore(WO-01): verify project setup acceptance criteria"
Then update .llm-mem/SESSION.md and .llm-mem/TODO.md with what you did.
Push everything.
```

### What to expect

The agent should:
1. Read all memory files and understand the project context
2. Check WO-01 acceptance criteria against what already exists
3. Fill in any gaps (most of the scaffold is already done)
4. Run tests and confirm they pass
5. Commit, update memory files, push

### After WO-01 completes

Move to the next work order. The pattern is the same every time:

```
Read .llm-mem/SESSION.md, DECISIONS.md, TODO.md.
Open docs/work-orders/WO-02-data-models.md.
Implement everything specified. Run tests. Commit and push.
Update memory files.
```

Work order execution order: **WO-01 → WO-02 → WO-03 → WO-04 → WO-04b → WO-05 → WO-06 → WO-07 → WO-08 → WO-09 → WO-10 → WO-11 → WO-12**

Do not skip ahead. Each work order builds on the previous ones.

## Step 6 — Session hygiene

### Starting a session

Always begin with:
```
Read AGENTS.md. Then read .llm-mem/SESSION.md, .llm-mem/DECISIONS.md, .llm-mem/TODO.md.
What's the current state of the project?
```

This ensures the agent has full context before doing anything.

### Ending a session

Always end with:
```
Run pytest tests/ -v to make sure everything passes.
Update .llm-mem/SESSION.md with what we did this session.
Update .llm-mem/TODO.md if any tasks changed.
Commit everything and push.
```

Or use the helper script:
```bash
python scripts/session_save.py --from-git
git add -A && git commit -m "chore: update session memory" && git push
```

### When the agent makes a design decision

Tell it:
```
Record this decision in .llm-mem/DECISIONS.md following the existing format.
```

## Step 7 — Using the prompt templates

For complex work orders, you can use the prompt templates in `docs/prompts/`:

### Architect review (before starting a work order)

```
Read docs/prompts/architect-review.md and use it to review WO-04 before we start implementing.
Produce a plan but do not write code yet.
```

### Implementation mode (during a work order)

```
Read docs/prompts/implementer-mode.md and use it while implementing WO-04.
Focus on one file at a time, commit after each.
```

### Test-fix loop (when tests fail)

```
Read docs/prompts/test-fix-mode.md. Tests are failing — diagnose and fix.
```

## Troubleshooting

### Tests fail on first run
Make sure you installed with dev dependencies: `pip install -e ".[dev]"` or `uv sync`.

### Ollama connection errors
The Ollama integration (WO-06) isn't built yet. If you see Ollama errors before WO-06, something is calling it prematurely — check which work order you're on.

### OpenCode can't find llm-mem CLI
Make sure you installed in the same Python environment OpenCode uses. If using `uv`, activate the virtual environment: `source .venv/bin/activate`.

### Agent forgets context mid-session
This is exactly the problem llm-mem solves. For now, re-paste the "read memory files" prompt. Once WO-05+ is complete, the MCP server handles this automatically.

## What success looks like

After completing all work orders, your setup will be:
1. OpenCode starts → llm-mem MCP server auto-starts as a subprocess
2. MCP server delivers a startup briefing with recent context
3. During coding, events are automatically captured and stored
4. Background Ollama model summarizes and compresses memories
5. Web UI at `localhost:8765` lets you browse, search, pin, and edit memories
6. Sensitive data is automatically detected and encrypted before storage

Until then, the bootstrap memory files (SESSION.md, DECISIONS.md, TODO.md) carry the project state between sessions.
