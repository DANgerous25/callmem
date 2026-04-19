"""Utilities for parsing JSON from LLM responses.

Local models frequently wrap JSON output in markdown code fences
(```json ... ```). These helpers strip the fences before parsing.
"""

from __future__ import annotations

import json
from typing import Any


def strip_code_fences(text: str) -> str:
    """Strip markdown code fences from an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def parse_json(response: str) -> Any:  # noqa: ANN401
    """Parse JSON from an LLM response, stripping code fences first.

    Returns the parsed object, or raises json.JSONDecodeError.
    """
    cleaned = strip_code_fences(response)
    return json.loads(cleaned)
