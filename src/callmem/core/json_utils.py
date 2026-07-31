"""Utilities for parsing JSON from LLM responses.

LLMs frequently wrap JSON output in markdown code fences (```json
... ```), sometimes with preamble or trailing commentary around the
fence, and sometimes with no fence at all. These helpers locate the
JSON payload before parsing it -- they do not attempt to repair
malformed or truncated JSON; a genuinely broken payload still raises.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```[^\n`]*\r?\n(.*?)```", re.DOTALL)

# Bound on how many '{'/'[' positions the no-fence fallback will try as
# candidate starts, so a bracket-heavy prose payload can't force a
# quadratic scan (each candidate attempt is itself O(len(text))).
_MAX_BALANCE_CANDIDATES = 25


def strip_code_fences(text: str) -> str:
    """Extract the JSON payload from an LLM response.

    Tries, in order:
      1. Fenced code blocks (```json, ```JSON, or bare ```) found
         anywhere in the text -- the first block whose contents parse
         as JSON is used.
      2. If no fenced block parses, the longest balanced ``{...}`` or
         ``[...]`` span that also parses as JSON on its own, among up
         to ``_MAX_BALANCE_CANDIDATES`` candidate start positions
         ('{' or '[' characters) scanned left to right. This is *not*
         simply the first bracket in the text: prose can contain
         incidental brackets that are themselves valid JSON but aren't
         the payload (e.g. "[1]" in "see refs [1] and [2] ... final
         answer: {...}"), so every candidate is checked and the
         longest one that parses wins, ties going to the earliest
         position. Braces/brackets inside string literals, including
         escaped quotes, are ignored while balancing so entity content
         containing ``}``/``]`` isn't corrupted.
      3. Otherwise the whitespace-trimmed input is returned unchanged,
         so callers see the same ``json.JSONDecodeError`` they always
         have.
    """
    text = text.strip()
    for block in _FENCE_RE.findall(text):
        candidate = block.strip()
        if _parses(candidate):
            return candidate
    balanced = _find_balanced_json(text)
    if balanced is not None:
        return balanced
    return text


def parse_json(response: str) -> Any:  # noqa: ANN401
    """Parse JSON from an LLM response, stripping code fences first.

    Returns the parsed object, or raises json.JSONDecodeError.
    """
    cleaned = strip_code_fences(response)
    return json.loads(cleaned)


def _parses(candidate: str) -> bool:
    """Return True if ``candidate`` is valid JSON on its own."""
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return False
    return True


def _find_balanced_json(text: str) -> str | None:
    """Return the longest balanced ``{...}``/``[...]`` span in ``text``
    that also parses as JSON on its own, trying up to
    ``_MAX_BALANCE_CANDIDATES`` candidate start positions in order.
    Returns None if no candidate both balances and parses.
    """
    candidates: list[str] = []
    attempts = 0
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        attempts += 1
        if attempts > _MAX_BALANCE_CANDIDATES:
            break
        span = _balanced_span(text, i)
        if span is not None and _parses(span):
            candidates.append(span)
    if not candidates:
        return None
    return max(candidates, key=len)


def _balanced_span(text: str, start: int) -> str | None:
    """Return the balanced span starting at ``text[start]`` (a '{' or
    '[' character), or None if it never closes. Depth is tracked only
    for the bracket type that opens the span; string literals (with
    escaped quotes) are skipped so their contents never affect bracket
    depth.
    """
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
