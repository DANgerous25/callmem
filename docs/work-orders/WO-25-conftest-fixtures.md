# WO-25 — Add Missing Conftest Fixtures

## Summary

`tests/conftest.py` has only 5 basic fixtures. UI and MCP integration tests define their own `TestClient` helpers locally. This leads to duplication and makes it harder to write new integration tests. Centralize shared fixtures.

## Files to Modify

- `tests/conftest.py` — add shared fixtures

## Fixtures to Add

1. `mock_ollama` — mocked `OllamaClient` (returns empty extractions by default, configurable)
2. `event_bus` — real or mock `EventBus` instance
3. `extractor` — `EntityExtractor` with `memory_db` and `mock_ollama`
4. `ui_client` — `TestClient` for the FastAPI app, with engine initialized
5. `populated_db` — in-memory database pre-seeded with sessions, events, and entities
6. `mcp_server` — MCP server instance for integration testing

## Acceptance Criteria

- [ ] All 6 fixtures added to `tests/conftest.py`
- [ ] Existing tests refactored to use shared fixtures where applicable (remove local `_make_client()` in test_ui.py, local server fixture in test_mcp_server.py)
- [ ] `pytest tests/ -v` — all tests pass after refactoring
