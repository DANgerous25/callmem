# WO-28 — False Positive Marking Tests + Ollama Client Tests

## Summary

False positive marking has only 2 unit tests. The Ollama client's `extract()` method has no dedicated tests. This work order fills both gaps.

## Files to Modify

- `tests/unit/test_sensitive_integration.py` — add more false positive tests
- `tests/unit/test_ollama_scan.py` — add `extract()` tests

## False Positive Test Cases

1. `test_false_positive_idempotent` — marking twice does not raise, stays false_positive=1
2. `test_false_positive_no_associated_event` — vault entry with null event_id is handled
3. `test_false_positive_restores_correct_value` — multi-finding event, only the marked one is un-redacted

## Ollama Client Test Cases

1. `test_extract_returns_response` — `extract()` returns `_generate()` output
2. `test_extract_returns_none_on_failure` — `_generate()` returns None
3. `test_extract_passes_prompt` — verify prompt is forwarded
4. `test_generate_handles_http_error` — non-200 response returns None
5. `test_parse_findings_non_list_json` — returns empty list
6. `test_parse_findings_missing_value_field` — skips invalid items

## Acceptance Criteria

- [ ] All false positive tests pass
- [ ] All Ollama client tests pass
- [ ] `pytest tests/unit/test_sensitive_integration.py tests/unit/test_ollama_scan.py -v` passes
