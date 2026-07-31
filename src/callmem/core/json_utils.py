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
      2. If no fenced block parses, the balanced ``{...}`` or
         ``[...]`` span whose CLOSING bracket appears latest (rightmost)
         in the text among candidates that both balance and parse as
         JSON on their own, trying up to ``_MAX_BALANCE_CANDIDATES``
         candidate start positions ('{' or '[' characters) scanned left
         to right; ties broken by longest span, then earliest start.
         Braces/brackets inside string literals, including escaped
         quotes, are ignored while balancing so entity content
         containing ``}``/``]`` isn't corrupted.

         Rightmost-closing, not first and not longest: every one of
         the six callers' prompts asks for JSON-only output, so a
         genuine payload is typically short, while the realistic
         deviation we have live evidence for is verbose content ahead
         of it (chain-of-thought, numbered references, step lists) --
         e.g. "[1]" in "see refs [1] and [2] ... final answer: {...}"
         is itself valid JSON and would win under "first", and
         "[1,2,...,12]" ahead of a short "{"a":1}" would win under
         "longest". Rightmost-end also keeps an enclosing object/array
         intact rather than returning a smaller nested fragment, since
         a container's closing bracket always appears after all of its
         children's. The one diagnostic case we don't have live
         evidence for -- a decoy appearing AFTER the real payload --
         would still win under this rule; every observed live failure
         (2/25 responses) was preamble-BEFORE-json, so that's the
         supported shape and this is an accepted residual gap, not an
         oversight.
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
    """Return the balanced ``{...}``/``[...]`` span in ``text`` whose
    closing bracket has the highest end index (i.e. closes latest,
    rightmost) among candidates that both balance and parse as JSON on
    their own, trying up to ``_MAX_BALANCE_CANDIDATES`` start positions
    in order. Ties broken by longest span, then earliest start.
    Returns None if no candidate both balances and parses.
    """
    candidates: list[tuple[int, int, int, str]] = []
    attempts = 0
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        attempts += 1
        if attempts > _MAX_BALANCE_CANDIDATES:
            break
        span = _balanced_span(text, i)
        if span is None or not _parses(span):
            continue
        end = i + len(span) - 1
        candidates.append((end, len(span), -i, span))
    if not candidates:
        return None
    return max(candidates)[-1]


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
