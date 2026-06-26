"""Auto-detection of ingestable content from assistant responses.

Lightweight regex-based classifier that detects decisions, discoveries,
failures, and TODOs in assistant messages. When a pattern matches, the
content is auto-ingested with the detected type so the agent doesn't
need to manually call mem_ingest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERN_GROUPS: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "decision",
        [
            re.compile(r"\b(let'?s go with|we'?ll use|we chose|decision:|going with|let'?s use)\b", re.I),
            re.compile(r"\b(the approach (is|will be)|final (approach|decision)|settled on)\b", re.I),
            re.compile(r"\b(switching to|migrating to|replacing .+ with)\b", re.I),
        ],
    ),
    (
        "discovery",
        [
            re.compile(r"\b(the issue was|turns out|i found that|the root cause is)\b", re.I),
            re.compile(r"\b(discovered|realized that|it turns out|the problem is)\b", re.I),
            re.compile(r"\b(confirmed that|verified that|the fix is)\b", re.I),
        ],
    ),
    (
        "failure",
        [
            re.compile(r"\b(this didn'?t work because|failed (to|because)|error:)\b", re.I),
            re.compile(r"\b(cannot|can'?t|unable to) .+ because\b", re.I),
            re.compile(r"\b(traceback|exception|segfault|crash(ed)?)\b", re.I),
        ],
    ),
    (
        "todo",
        [
            re.compile(r"\b(we need to|next step is|still need to|todo:|to-do:)\b", re.I),
            re.compile(r"\b(should (also )?(implement|add|fix|update|create))\b", re.I),
            re.compile(r"\b(remaining work|outstanding (tasks?|items?))\b", re.I),
        ],
    ),
]


@dataclass
class DetectedIngestion:
    """A single auto-detected ingestion candidate."""
    type: str
    content: str
    pattern_matched: str


def detect_ingestable_content(text: str) -> list[DetectedIngestion]:
    """Scan text for patterns indicating ingestable content.

    Returns a list of detected ingestion candidates. Each candidate
    includes the detected type and a snippet of the matching context
    (the sentence containing the match, truncated to 500 chars).
    """
    if not text or len(text) < 10:
        return []

    results: list[DetectedIngestion] = []
    sentences = _split_sentences(text)

    for entity_type, patterns in _PATTERN_GROUPS:
        for pattern in patterns:
            for sentence in sentences:
                match = pattern.search(sentence)
                if match:
                    snippet = sentence.strip()[:500]
                    if not any(
                        r.type == entity_type and r.content == snippet
                        for r in results
                    ):
                        results.append(DetectedIngestion(
                            type=entity_type,
                            content=snippet,
                            pattern_matched=match.group(0),
                        ))
                    break

    return results


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving context around matches."""
    parts = re.split(r'(?<=[.!?])\s+|\n+', text)
    return [p.strip() for p in parts if len(p.strip()) > 5]
