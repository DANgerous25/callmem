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


def strip_code_fences(text: str) -> str:
    """Extract the JSON payload from an LLM response.

    Tries, in order:
      1. Fenced code blocks (```json, ```JSON, or bare ```) found
         anywhere in the text -- the first block whose contents parse
         as JSON is used.
      2. If no fenced block parses, the first balanced JSON object or
         array found anywhere in the text (braces/brackets inside
         string literals, including escaped quotes, are ignored so
         entity content containing ``}``/``]`` isn't corrupted).
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
    """Return the first balanced ``{...}`` or ``[...]`` span in
    ``text``, or None if none is found. Depth is tracked only for the
    bracket type that opens the span; string literals (with escaped
    quotes) are skipped so their contents never affect bracket depth.
    """
    start = -1
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start == -1:
        return None

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
