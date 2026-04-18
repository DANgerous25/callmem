# WO-22 — Re-extraction Command

## Goal

Add a CLI command to re-process existing events through the extraction pipeline with the current model, so users can upgrade extraction quality after changing models without re-importing sessions.

## Background

When a user switches from a smaller model (e.g. gemma4:e4b) to a larger one (e.g. qwen3:14b), all previously extracted entities were produced by the old model. There's no way to retroactively improve them without re-importing from scratch — which is slow and loses any manual edits (pins, status changes, resolutions).

A re-extraction command would re-run entity extraction on existing raw events using the currently configured model, replacing or supplementing the old entities.

## Deliverables

### 1. CLI command: `llm-mem re-extract`

```bash
# Re-extract all events (full rebuild of entities)
llm-mem re-extract --project ~/ellma-trading-bot

# Re-extract only a specific session
llm-mem re-extract --session ses_2693f3fc --project ~/ellma-trading-bot

# Re-extract events from the last N days
llm-mem re-extract --since 7d --project ~/ellma-trading-bot

# Dry run — show what would be re-extracted without doing it
llm-mem re-extract --dry-run --project ~/ellma-trading-bot

# Limit concurrency to avoid GPU overload
llm-mem re-extract --batch-size 5 --project ~/ellma-trading-bot
```

### 2. Re-extraction logic

a) **Preserve user edits** — entities that have been manually pinned, had their status changed (e.g. TODO marked done), or been resolved should NOT be overwritten. Options:
   - Flag: `--preserve-edits` (default: true) — skip entities with manual modifications
   - Flag: `--force` — overwrite everything including edited entities

b) **Archive old entities** — before replacing, archive the old entity (set `archived_at` timestamp). This preserves history and allows rollback if the new model produces worse results.

c) **Batch processing** — process events in batches (default: extraction batch_size from config). Show progress:
   ```
   Re-extracting with qwen3:14b (num_ctx: auto)
   
   Session 1/30: ses_2693f3fc (179 events)
     Batch 1/18... 3 entities extracted
     Batch 2/18... 5 entities extracted
     ...
   
   Progress: 12/30 sessions, 847 events processed, 234 entities created
   ```

d) **Resume support** — if interrupted (Ctrl+C, OOM, etc.), track progress so `re-extract` can resume where it left off. Use a marker in the jobs table or a dedicated re-extraction state table.

e) **Comparison mode** (nice-to-have) — `--compare` flag that extracts with the new model but doesn't replace, instead showing a side-by-side diff of old vs new entity quality for a sample of events. Helps the user decide if re-extraction is worth it before committing.

### 3. Safety

- Show a confirmation prompt before starting:
  ```
  Re-extract 1788 events across 30 sessions using qwen3:14b?
  Estimated time: ~15 minutes (based on batch size and model speed)
  Existing entities will be archived (not deleted).
  
  Proceed? [y/N]:
  ```
- Respect `--dry-run` to show scope without executing
- Never delete entities — only archive and create new ones
- If Ollama is unreachable, fail fast with a clear error

## Constraints

- Python 3.10 compatible
- No AI attribution
- Must work while the daemon is running (don't corrupt the database — use WAL mode transactions)
- Should not interfere with live extraction of new events (use separate job type in the queue)

## Acceptance criteria

- [ ] `llm-mem re-extract` processes all events and creates new entities
- [ ] `--session` flag limits scope to a single session
- [ ] `--since` flag limits scope by time
- [ ] `--dry-run` shows scope without executing
- [ ] `--preserve-edits` (default) skips manually modified entities
- [ ] `--force` overwrites all entities
- [ ] Old entities are archived with timestamp, not deleted
- [ ] Progress is displayed during execution
- [ ] Interrupted re-extraction can be resumed
- [ ] Works concurrently with running daemon
- [ ] All existing tests pass

## Suggested tests

- Unit test: re-extraction skips pinned/edited entities when `--preserve-edits`
- Unit test: re-extraction archives old entities before creating new ones
- Unit test: `--since` and `--session` filters work correctly
- Unit test: `--dry-run` doesn't modify the database
- Integration test: re-extract a session and verify new entities are created with current model
