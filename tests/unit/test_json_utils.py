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
import time

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


class TestBalancedScanRetriesCandidates:
    """Round-1 review fix: the balanced scan must not give up (or
    silently return the wrong value) just because the first '{'/'['
    in the text doesn't turn out to be the real payload. It tries
    every candidate start position (bounded) and picks the longest
    one that both balances and parses as JSON on its own -- "longest",
    not "first", because incidental prose brackets like "[1]" in a
    numbered list are themselves valid JSON and would otherwise win
    by mere position."""

    def test_first_bracket_span_invalid_later_one_valid(self) -> None:
        # "{1,2,3}" balances but isn't valid JSON (bare numeric keys);
        # the real payload comes after it.
        raw = (
            "Looking at steps {1,2,3} in the analysis, the verdict "
            'is: {"resolved": true, "reason": "done"}'
        )
        assert parse_json(raw) == {"resolved": True, "reason": "done"}

    def test_first_bracket_span_trivially_valid_but_not_the_payload(
        self,
    ) -> None:
        # "[1]" and "[2]" are each independently valid JSON (a
        # single-element array), so a naive "first candidate that
        # parses" would wrongly return [1]. The real payload is the
        # longer object later in the text.
        raw = (
            "See references [1] and [2] for details. "
            'Final answer: {"resolved": true}'
        )
        assert parse_json(raw) == {"resolved": True}

    def test_nothing_parses_still_raises(self) -> None:
        raw = "Looking at steps {1,2,3} and refs [a, b] with no real JSON."
        with pytest.raises(json.JSONDecodeError):
            parse_json(raw)

    def test_bracket_heavy_payload_bounded_by_candidate_cap(self) -> None:
        # 20000 unmatched '{' characters: each candidate attempt scans
        # to the end of the text without balancing. Without a cap on
        # the number of candidate starts tried, this is quadratic;
        # with the cap it must stay fast and still raise (nothing
        # balances, so nothing parses).
        raw = "{" * 20_000
        start = time.monotonic()
        with pytest.raises(json.JSONDecodeError):
            parse_json(raw)
        assert time.monotonic() - start < 2.0


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
        # No nested candidate exists here to fall back to (unlike a
        # payload with a truncated outer object around a complete
        # inner array/object, which legitimately yields that inner
        # value under the multi-candidate scan) -- this is truncated
        # with nothing else parseable, so it must still raise.
        with pytest.raises(json.JSONDecodeError):
            parse_json('{"a": 1, "b": 2')

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
