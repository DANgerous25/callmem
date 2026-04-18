# WO-23 — Extraction Tests: Null event_bus Handling

## Summary

The `EntityExtractor` accepts `event_bus: Any | None = None` and guards against `None` before calling `publish()`. However, **no test explicitly exercises the `event_bus=None` code path** during entity creation. All existing tests pass because they never trigger the publish branch. This work order closes that gap.

## Problem

1. No test verifies that entity extraction works without error when `event_bus=None` and entities are actually created.
2. The `_setup_engine_and_extractor` test helper creates an `EntityExtractor` without wiring it to the engine's `event_bus`, so integration between engine and extractor event_bus is never tested.

## Files to Modify

- `tests/unit/test_extraction.py` — add test for `event_bus=None` during extraction
- `tests/conftest.py` — add `extractor` fixture (if not already added in WO-25)

## Acceptance Criteria

- [ ] Test explicitly creates `EntityExtractor` with `event_bus=None`, triggers entity creation, and asserts no `AttributeError`
- [ ] Test creates `EntityExtractor` with a mock `event_bus`, triggers entity creation, and asserts `event_bus.publish()` was called
- [ ] All existing tests continue to pass
- [ ] `pytest tests/unit/test_extraction.py -v` passes
