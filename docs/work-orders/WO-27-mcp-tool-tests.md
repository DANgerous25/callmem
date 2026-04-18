# WO-27 — MCP Tool Tests: Missing Coverage + Vault Review Tool

## Summary

5 of 11 MCP tools have no integration tests: `mem_get_briefing`, `mem_search_index`, `mem_timeline`, `mem_get_entities`, `mem_search_by_file`. Additionally, the `mem_vault_review` tool referenced in `docs/sensitive-data.md` does not exist yet.

## Files to Modify

- `src/llm_mem/mcp/tools.py` — add `mem_vault_review` tool definition and handler
- `src/llm_mem/mcp/server.py` — register new tool (if needed)
- `tests/integration/test_mcp_server.py` — add tests for all untested tools

## New Tool: `mem_vault_review`

Allows marking a vault entry as false positive via MCP:
- Input: `vault_id` (string)
- Calls `engine.mark_false_positive(vault_id)`
- Returns success/failure message

## Test Cases to Add

### Existing tools (integration tests):
1. `test_get_briefing` — returns briefing with context economics
2. `test_search_index` — returns compact index table
3. `test_timeline` — returns timeline around anchor
4. `test_get_entities` — returns full entity details
5. `test_search_by_file` — returns entities for file path
6. `test_vault_review_false_positive` — marks vault entry as false positive
7. `test_vault_review_nonexistent` — returns error for bad ID

## Acceptance Criteria

- [ ] `mem_vault_review` tool implemented and registered
- [ ] All 7 new integration tests pass
- [ ] Existing MCP tests still pass
- [ ] `pytest tests/integration/test_mcp_server.py -v` passes
