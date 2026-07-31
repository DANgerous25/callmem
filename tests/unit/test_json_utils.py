"""Tests for the shared LLM-JSON parser.

``parse_json``/``strip_code_fences`` feed six call sites (extraction,
staleness, resolve_judge, consolidation, and both sensitive-data
scanners). A live diagnostic against the real backend found 2/25
responses shaped like::

    Here is the analysis:
    ```json
    {...}
    ```

``strip_code_fences`` only stripped a fence when the response
*started* with ``` -- so preamble-prefixed fences fell straight
through to ``json.loads`` and raised on the prose. In the extraction
path this silently returned ``{}``, i.e. entity loss with no error.
These tests pin down the fix: fenced blocks found anywhere in the
text, and a brace/bracket-balanced fallback when there is no fence at
all, without ever attempting to repair genuinely broken JSON.
"""

from __future__ import annotations

import json

import pytest

from callmem.core.json_utils import parse_json, strip_code_fences


class TestFencedAnywhereInText:
    """Requirement 1: a fenced block anywhere in the text is found."""

    def test_live_captured_preamble_then_json_fence(self) -> None:
        # Captured verbatim shape from the live diagnostic.
        raw = 'Here is the analysis:\n```json\n{"a": 1, "b": 2}\n```'
        assert parse_json(raw) == {"a": 1, "b": 2}

    def test_uppercase_json_language_tag(self) -> None:
        raw = '```JSON\n{"a": 1}\n```'
        assert parse_json(raw) == {"a": 1}

    def test_bare_fence_no_language_tag(self) -> None:
        raw = '```\n{"a": 1}\n```'
        assert parse_json(raw) == {"a": 1}

    def test_trailing_prose_after_closing_fence(self) -> None:
        raw = (
            'Here is the analysis:\n```json\n{"a": 1}\n```\n'
            "Hope that helps!"
        )
        assert parse_json(raw) == {"a": 1}

    def test_multiple_fences_first_non_json_second_valid(self) -> None:
        raw = (
            "Sure, here's some context:\n"
            "```\n"
            "this is not json, just an example\n"
            "```\n"
            "and here is the real payload:\n"
            "```json\n"
            '{"a": 1, "b": 2}\n'
            "```"
        )
        assert parse_json(raw) == {"a": 1, "b": 2}


class TestNoFenceBalancedScan:
    """Requirement 2: no-fence fallback to balanced-scan JSON."""

    def test_no_fence_with_preamble(self) -> None:
        raw = 'Here is the result: {"a": 1, "b": 2} -- done.'
        assert parse_json(raw) == {"a": 1, "b": 2}

    def test_no_fence_array_with_preamble(self) -> None:
        raw = "The findings are: [1, 2, 3] as requested."
        assert parse_json(raw) == [1, 2, 3]

    def test_string_values_with_braces_brackets_backticks_escapes(
        self,
    ) -> None:
        # Guards against a naive rfind/index-based scan, which would
        # corrupt real callmem entity content containing these chars.
        raw = (
            "noise before "
            '{"content": "closes with } and ] and ` marks, '
            'and \\"quoted\\" inside"} '
            "noise after"
        )
        result = parse_json(raw)
        assert result == {
            "content": (
                "closes with } and ] and ` marks, "
                'and "quoted" inside'
            )
        }

    def test_nested_object_and_array_balance_correctly(self) -> None:
        raw = (
            "preamble\n"
            '{"a": [1, 2, {"b": 3}], "c": {"d": [4, 5]}}\n'
            "trailing"
        )
        assert parse_json(raw) == {
            "a": [1, 2, {"b": 3}],
            "c": {"d": [4, 5]},
        }


class TestUnchangedBehaviour:
    """Requirements 3 and the "don't guess" boundary."""

    def test_already_clean_json_byte_identical(self) -> None:
        raw = '{"a": 1, "b": [1, 2, 3]}'
        assert strip_code_fences(raw) == raw

    def test_already_clean_json_with_surrounding_whitespace(self) -> None:
        raw = '  \n {"a": 1}\n  '
        assert strip_code_fences(raw) == '{"a": 1}'

    def test_malformed_json_still_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_json("this is not json at all, no braces here")

    def test_unbalanced_braces_still_raise(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_json('{"a": 1, "b": [1, 2, 3]')

    def test_empty_string_still_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_json("")

    def test_whitespace_only_still_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_json("   \n\t  ")

    def test_empty_fenced_block_still_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_json("```json\n```")

    def test_does_not_repair_truncated_json_inside_fence(self) -> None:
        raw = '```json\n{"a": 1, "b": [1, 2\n```'
        with pytest.raises(json.JSONDecodeError):
            parse_json(raw)


class TestExtractionRegression:
    """Regression: the extraction call site must recover real items
    from a preamble-wrapped realistic payload, not silently return
    ``{}`` as it did before this fix (the production-impacting case:
    zero entities extracted for the affected batch)."""

    def test_parse_extraction_recovers_items_from_preamble_wrapped_payload(
        self, extractor: object,
    ) -> None:
        raw = (
            "Here is the extraction:\n"
            "```json\n"
            "{"
            '"decisions": [{"title": "Use Redis", '
            '"content": "Chose Redis for caching"}], '
            '"todos": [], "facts": [], "failures": [], '
            '"discoveries": [], "features": [], "bugfixes": [], '
            '"research": [], "changes": []'
            "}\n"
            "```\n"
            "Let me know if you need anything else."
        )
        result = extractor._parse_extraction(raw)  # type: ignore[attr-defined]
        assert result["decisions"] == [
            {"title": "Use Redis", "content": "Chose Redis for caching"}
        ]
