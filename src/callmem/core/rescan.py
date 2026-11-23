"""Re-scan already-stored memory for secrets and redact them in place.

Rows written before the local pre-scanner existed were only pattern-checked
(or checked by a remote model), and entities/summaries were produced by the
main backend from that text. This walks every free-text column, runs the
pattern scanner plus the local pre-scanner, redacts in place, and vaults
the originals — the same treatment new events get at ingest.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ulid import ULID

from callmem.core.redaction import (
    Detection,
    PatternScanner,
    apply_redactions,
    merge_detections,
    plausible_llm_finding,
    scan_in_pieces,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from callmem.core.repository import Repository
    from callmem.models.config import Config

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
SCAN_ATTEMPTS = 3  # a long run shouldn't stop on one slow or garbled reply
CHUNK_OVERLAP = 200
SEPARATOR = "\n\n---\n\n"

Row = tuple[int, str, str]  # rowid, project_id, text


class ScannerUnavailableError(RuntimeError):
    """The pre-scanner didn't answer; the run stops so it can resume later."""


@dataclass
class RescanStats:
    """What a run looked at and changed. Never holds secret values."""

    rows_scanned: Counter[str] = field(default_factory=Counter)
    rows_changed: Counter[str] = field(default_factory=Counter)
    detections: Counter[str] = field(default_factory=Counter)
    conflicts: int = 0
    llm_calls: int = 0
    changed_entities: dict[str, str] = field(default_factory=dict)  # id -> project_id


class Rescanner:
    """Walks every re-scannable text column, resumable via a progress file."""

    def __init__(
        self,
        repo: Repository,
        scanner: Any,
        config: Config,
        vault_dir: Path,
        progress_path: Path | None = None,
        dry_run: bool = False,
        batch_chars: int = 8000,
        limit: int | None = None,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> None:
        self.repo = repo
        self.scanner = scanner
        self.confidence = config.sensitive_data.llm_scan_confidence
        self.vault_dir = vault_dir
        self.progress_path = progress_path
        self.dry_run = dry_run
        self.batch_chars = batch_chars
        self.limit = limit
        self.on_progress = on_progress
        self.patterns = PatternScanner()
        self.known_values: dict[str, str] = {}  # value -> category
        self.stats = RescanStats()

    def run(self) -> RescanStats:
        """Scan every field. Raises ScannerUnavailableError if the scanner drops."""
        progress = self._load_progress()
        for table, column in self.repo.TEXT_FIELDS:
            self._scan_field(table, column, progress)
        return self.stats

    # ── Walking ──────────────────────────────────────────────────────

    def _scan_field(self, table: str, column: str, progress: dict[str, int]) -> None:
        key = f"{table}.{column}"
        after = progress.get(key, 0)
        seen = 0
        while self.limit is None or seen < self.limit:
            rows = self.repo.list_text_rows(table, column, after, PAGE_SIZE)
            if self.limit is not None:
                rows = rows[: self.limit - seen]
            if not rows:
                break
            for group in self._group_rows(rows):
                self._process_group(table, column, group)
                after = group[-1][0]
                progress[key] = after
                self._save_progress(progress)
            seen += len(rows)
            if self.on_progress is not None:
                self.on_progress(key, self.stats.rows_scanned[key])

    def _group_rows(self, rows: list[Row]) -> list[list[Row]]:
        """Pack consecutive rows into groups that fit one scan call."""
        groups: list[list[Row]] = []
        current: list[Row] = []
        size = 0
        for row in rows:
            n = len(row[2]) + len(SEPARATOR)
            if current and size + n > self.batch_chars:
                groups.append(current)
                current, size = [], 0
            current.append(row)
            size += n
        if current:
            groups.append(current)
        return groups

    def _scan_texts(self, texts: list[str]) -> list[str]:
        """Scan texts as one call (or chunked, if a single text is too long).

        Returns the sensitive values found.
        """
        joined = SEPARATOR.join(texts)
        step = self.batch_chars - CHUNK_OVERLAP
        pieces = (
            [joined] if len(joined) <= self.batch_chars
            else [joined[i:i + self.batch_chars] for i in range(0, len(joined), step)]
        )
        values: list[str] = []
        for piece in pieces:
            hits = None
            for _ in range(SCAN_ATTEMPTS):
                hits = scan_in_pieces(self._counted_scan, piece)
                if hits is not None:
                    break
            if hits is None:
                raise ScannerUnavailableError("pre-scanner did not answer")
            for h in hits:
                v = h.original_value
                if h.confidence >= self.confidence and plausible_llm_finding(
                    v, h.category, self.patterns.detect_categories,
                ):
                    values.append(v)
                    self.known_values.setdefault(v, h.category)
        return values

    def _counted_scan(self, text: str) -> list[Detection] | None:
        self.stats.llm_calls += 1
        return self.scanner.scan_sensitive_strict(text)

    # ── Redacting ────────────────────────────────────────────────────

    def _process_group(self, table: str, column: str, group: list[Row]) -> None:
        key = f"{table}.{column}"
        staged = []
        for rowid, project_id, text in group:
            dets = merge_detections(
                self.patterns.scan(text), self._find_values(text, self.known_values),
            )
            staged.append((rowid, project_id, text, dets, apply_redactions(text, dets)))
        found = self._scan_texts([s[4] for s in staged])
        found_map = {v: self.known_values[v] for v in found}
        for rowid, project_id, original, dets, stage1 in staged:
            self.stats.rows_scanned[key] += 1
            llm_dets = self._find_values(stage1, found_map)
            final = apply_redactions(stage1, llm_dets) if llm_dets else stage1
            if final == original:
                continue
            self.stats.rows_changed[key] += 1
            if self.dry_run:
                self._count(dets + llm_dets)
                continue
            self._write(table, column, rowid, project_id, original, final, dets + llm_dets)

    def _find_values(self, text: str, values: dict[str, str]) -> list[Detection]:
        """Detections for every occurrence of each known value in ``text``."""
        found: list[Detection] = []
        for value, category in values.items():
            start = text.find(value)
            while start != -1:
                found.append(Detection(
                    vault_id=str(ULID()), category=category, detector="llm",
                    pattern_name="rescan", original_value=value,
                    start=start, end=start + len(value), confidence=1.0,
                ))
                start = text.find(value, start + len(value))
        return merge_detections(found, []) if found else []

    def _write(
        self, table: str, column: str, rowid: int, project_id: str,
        original: str, final: str, detections: list[Detection],
    ) -> None:
        if not self.repo.update_text_if_unchanged(table, column, rowid, original, final):
            # Edited since we read it (live daemon); the next run picks it up.
            self.stats.conflicts += 1
            return
        from callmem.core.crypto import VaultKeyManager

        crypto = VaultKeyManager(self.vault_dir)
        row_id = self.repo.text_row_id(table, rowid)
        for d in detections:
            self.repo.insert_vault_entry(
                id=d.vault_id, project_id=project_id, category=d.category,
                detector=d.detector, pattern_name=d.pattern_name,
                ciphertext=crypto.encrypt(d.original_value),
                event_id=row_id if table == "events" else None,
            )
        self._count(detections)
        if table == "entities" and row_id is not None:
            self.stats.changed_entities[row_id] = project_id

    def _count(self, detections: list[Detection]) -> None:
        for d in detections:
            self.stats.detections[d.detector] += 1

    # ── Progress ─────────────────────────────────────────────────────

    def _load_progress(self) -> dict[str, int]:
        if self.dry_run or self.progress_path is None or not self.progress_path.exists():
            return {}
        try:
            return {k: int(v) for k, v in json.loads(self.progress_path.read_text()).items()}
        except (ValueError, OSError, AttributeError) as exc:
            logger.warning("Ignoring unreadable rescan progress %s: %s", self.progress_path, exc)
            return {}

    def _save_progress(self, progress: dict[str, int]) -> None:
        if self.dry_run or self.progress_path is None:
            return
        tmp = self.progress_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(progress))
        tmp.replace(self.progress_path)
