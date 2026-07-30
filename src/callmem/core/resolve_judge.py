"""LLM-judged verification for the retroactive ``callmem resolve`` sweep.

The keyword matcher in ``extraction.py`` (``_resolve_by_drivers``) is a
recall stage only: it finds candidate driver/target pairs, not proof that
the driver actually completed the target. A live 69-item triage showed the
matcher alone would have been wrong on 46% of its matches -- 24 targets had
evidence CONTRADICTING the match, 8 were uncertain. This module is the
precision stage that sits after recall: every candidate pair is judged
against its own evidence (driver title+content+source-event text, target
title+content) in batched LLM calls, chunked ~10 pairs per call (mirrors
``consolidation.py``'s judge conventions).

Verdict per pair:
  - CONFIRMED    - evidence shows the target's work was actually completed.
    ONLY this verdict closes the target, via the unified
    ``Repository.mark_resolved``.
  - CONTRADICTED - evidence shows the target's problem persists or the
    driver only discusses it. Left open.
  - UNCERTAIN    - not enough evidence either way. Left open.

Fail-open is the hard requirement: any judge response that isn't exactly
the expected JSON shape -- one verdict per pair, referencing every pair
number in the chunk exactly once -- is treated as a bad LLM day. The whole
chunk is marked UNCERTAIN, the failure is logged loudly, and nothing in
that chunk is ever closed. The sweep must never close on a bad LLM day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from callmem.core.json_utils import parse_json
from callmem.core.prompts import RESOLVE_JUDGE_PROMPT
from callmem.core.repository import Repository

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.models.config import Config

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"CONFIRMED", "CONTRADICTED", "UNCERTAIN"}
_CHUNK_SIZE = 10
_MAX_TEXT_CHARS = 400


@dataclass
class ResolutionCandidate:
    """One driver/target pair produced by the recall stage, carrying
    every field the judge prompt needs -- assembled once up front (batched
    fetches) rather than per-candidate."""

    driver_id: str
    driver_title: str
    driver_content: str
    driver_source_text: str
    target_id: str
    target_type: str
    target_title: str
    target_content: str


@dataclass
class ResolveSweepStats:
    """Per-run counters for the judged sweep."""

    confirmed: int = 0
    contradicted: int = 0
    uncertain: int = 0
    judge_failed: bool = False


class ResolutionJudge:
    """Judges recall-stage candidates and closes only CONFIRMED targets."""

    def __init__(
        self, db: Database, llm: Any, config: Config | None = None,
    ) -> None:
        self.db = db
        self.repo = Repository(db)
        self.llm = llm
        self.config = config

    def run(
        self,
        candidates: list[ResolutionCandidate],
        dry_run: bool = False,
    ) -> tuple[list[dict[str, Any]], ResolveSweepStats]:
        """Judge every candidate in chunks of ``_CHUNK_SIZE`` and, unless
        ``dry_run``, close every CONFIRMED target via
        ``Repository.mark_resolved``. CONTRADICTED/UNCERTAIN candidates
        are never closed. Returns per-pair records (for CLI rendering)
        and the run's aggregate stats.
        """
        stats = ResolveSweepStats()
        records: list[dict[str, Any]] = []

        for i in range(0, len(candidates), _CHUNK_SIZE):
            chunk = candidates[i:i + _CHUNK_SIZE]
            verdicts = self._judge_chunk(chunk)
            if verdicts is None:
                stats.judge_failed = True
                verdicts = ["UNCERTAIN"] * len(chunk)

            for candidate, verdict in zip(chunk, verdicts, strict=True):
                record: dict[str, Any] = {
                    "id": candidate.target_id,
                    "type": candidate.target_type,
                    "title": candidate.target_title,
                    "driver_title": candidate.driver_title,
                    "verdict": verdict,
                }
                if verdict == "CONFIRMED":
                    stats.confirmed += 1
                    resolved_status = (
                        "done" if candidate.target_type == "todo" else "resolved"
                    )
                    record["status"] = resolved_status
                    if not dry_run:
                        self.repo.mark_resolved(
                            candidate.target_id, resolved_status,
                        )
                elif verdict == "CONTRADICTED":
                    stats.contradicted += 1
                else:
                    stats.uncertain += 1
                records.append(record)

        return records, stats

    def _judge_chunk(
        self, chunk: list[ResolutionCandidate],
    ) -> list[str] | None:
        """Judge one chunk, returning one verdict per candidate in chunk
        order, or None on any malformed/absent response (fail-open)."""
        if self.llm is None:
            return None

        prompt = RESOLVE_JUDGE_PROMPT.format(pairs_block=_format_pairs(chunk))
        raw = self.llm.extract(prompt)
        parsed = _parse(raw, len(chunk))
        if parsed is None:
            logger.warning(
                "Resolve-sweep judge returned malformed or absent output "
                "for %d pair(s) -- leaving all as UNCERTAIN (fail-open). "
                "Raw response: %r",
                len(chunk), (raw or "")[:200],
            )
            return None
        return [parsed[pair] for pair in range(1, len(chunk) + 1)]


def _truncate(value: str | None, limit: int = _MAX_TEXT_CHARS) -> str:
    value = value or ""
    return value if len(value) <= limit else value[:limit] + "..."


def _format_pairs(chunk: list[ResolutionCandidate]) -> str:
    parts = []
    for idx, c in enumerate(chunk, start=1):
        parts.append(
            f"{idx}. DRIVER (candidate completion) [{c.driver_id}] "
            f"{c.driver_title!r}\n"
            f"   content: {_truncate(c.driver_content)!r}\n"
            f"   source evidence: {_truncate(c.driver_source_text)!r}\n"
            f"   TARGET (open {c.target_type}) [{c.target_id}] "
            f"{c.target_title!r}\n"
            f"   content: {_truncate(c.target_content)!r}"
        )
    return "\n\n".join(parts)


def _parse(raw: str | None, n: int) -> dict[int, str] | None:
    """Validate the judge's JSON for a chunk of ``n`` pairs: a list of
    exactly ``n`` objects, each naming a distinct pair number in
    ``1..n`` with a valid verdict. Any deviation returns None -- the
    caller treats that as fail-open."""
    if not raw:
        return None
    try:
        data = parse_json(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or len(data) != n:
        return None

    verdicts: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict):
            return None

        pair = item.get("pair")
        if isinstance(pair, bool) or not isinstance(pair, int):
            return None
        if pair < 1 or pair > n or pair in verdicts:
            return None

        verdict = str(item.get("verdict", "")).strip().upper()
        if verdict not in _VALID_VERDICTS:
            return None

        verdicts[pair] = verdict

    if len(verdicts) != n:
        return None
    return verdicts
