# WO-26 — Settings Route Integration Tests

## Summary

The settings route (`/settings`, `POST /settings`, `/partials/briefing-preview`) is fully implemented in `src/llm_mem/ui/routes/settings.py` but has **zero integration tests**. This work order adds comprehensive test coverage.

## Files to Modify

- `tests/integration/test_ui.py` — add `TestSettings` class

## Test Cases

### `TestSettings`
1. `test_settings_page_loads` — GET `/settings` returns 200, contains form fields
2. `test_settings_page_shows_current_config` — form is pre-populated with current values
3. `test_settings_save` — POST `/settings` with form data, redirects or shows success
4. `test_settings_save_creates_backup` — verify `.bak` file created
5. `test_briefing_preview` — GET `/partials/briefing-preview` returns fragment
6. `test_briefing_preview_with_max_tokens` — respects `max_tokens` query param

## Acceptance Criteria

- [ ] All 6 test cases pass
- [ ] `pytest tests/integration/test_ui.py -v` passes
- [ ] Tests use shared `ui_client` fixture from conftest (WO-25)
