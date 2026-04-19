"""Knowledge agents — queryable memory brains.

Builds corpora from filtered entities and answers questions
using the local Ollama model.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from callmem.compat import UTC
from callmem.core.prompts import KNOWLEDGE_QUERY_PROMPT
from callmem.core.retrieval import _estimate_tokens
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.ollama import OllamaClient

logger = logging.getLogger(__name__)

MAX_CORPUS_TOKENS = 30000


class KnowledgeAgent:
    """Build and query corpora of project observations."""

    def __init__(self, db: Database, ollama: OllamaClient) -> None:
        self.db = db
        self.ollama = ollama

    def build_corpus(
        self,
        name: str,
        project_id: str | None = None,
        types: list[str] | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        file_paths: list[str] | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Build a corpus from filtered entities."""
        entities = self._fetch_entities(
            project_id, types, date_start, date_end, query
        )

        if file_paths:
            entities = self._filter_by_files(entities, file_paths)

        total_tokens = sum(
            _estimate_tokens(e.get("content") or "") for e in entities
        )

        corpus_id = self._save_corpus(
            name, project_id, types, date_start, date_end,
            file_paths, query, len(entities), total_tokens,
        )
        self._save_corpus_entities(corpus_id, entities)

        return {
            "id": corpus_id,
            "name": name,
            "entity_count": len(entities),
            "token_count": total_tokens,
            "warning": (
                f"Corpus exceeds {MAX_CORPUS_TOKENS} tokens — "
                "queries may be slow or truncated"
            ) if total_tokens > MAX_CORPUS_TOKENS else None,
        }

    def list_corpora(self) -> list[dict[str, Any]]:
        """List all corpora with stats."""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM corpora ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def query_corpus(self, corpus_name: str, question: str) -> str:
        """Ask a question against a corpus."""
        entities = self._load_corpus_entities(corpus_name)
        if not entities:
            return "Corpus is empty or not found."

        context = self._format_corpus_context(entities)
        prompt = KNOWLEDGE_QUERY_PROMPT.format(
            context=context, question=question
        )

        response = self.ollama._generate(prompt)
        if response is None:
            return "LLM unavailable — cannot answer query."
        return response

    def rebuild_corpus(self, corpus_name: str) -> dict[str, Any]:
        """Rebuild corpus with latest entities matching original filters."""
        corpus = self._get_corpus_by_name(corpus_name)
        if corpus is None:
            msg = f"Corpus not found: {corpus_name}"
            raise ValueError(msg)

        filters = json.loads(corpus["filters"])
        conn = self.db.connect()
        try:
            conn.execute(
                "DELETE FROM corpus_entities WHERE corpus_id = ?",
                (corpus["id"],),
            )
            conn.execute(
                "DELETE FROM corpora WHERE id = ?",
                (corpus["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        return self.build_corpus(
            name=corpus_name,
            project_id=corpus.get("project_id"),
            **filters,
        )

    def delete_corpus(self, corpus_name: str) -> None:
        """Delete a corpus by name."""
        conn = self.db.connect()
        try:
            conn.execute(
                "DELETE FROM corpora WHERE name = ?",
                (corpus_name,),
            )
            conn.commit()
        finally:
            conn.close()

    def _fetch_entities(
        self,
        project_id: str | None,
        types: list[str] | None,
        date_start: str | None,
        date_end: str | None,
        query: str | None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"type IN ({placeholders})")
            params.extend(types)
        if date_start:
            clauses.append("created_at >= ?")
            params.append(date_start)
        if date_end:
            clauses.append("created_at <= ?")
            params.append(date_end)

        where = " AND ".join(clauses) if clauses else "1=1"

        conn = self.db.connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM entities WHERE {where} "
                "ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()
            results = [dict(r) for r in rows]
        finally:
            conn.close()

        if query:
            q = query.lower()
            results = [
                r for r in results
                if q in (r.get("title") or "").lower()
                or q in (r.get("content") or "").lower()
            ]

        return [dict(Entity.from_row(r).to_row()) for r in results]

    def _filter_by_files(
        self,
        entities: list[dict[str, Any]],
        file_paths: list[str],
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            valid_ids = set()
            for fp in file_paths:
                rows = conn.execute(
                    "SELECT entity_id FROM entity_files WHERE file_path = ?",
                    (fp,),
                ).fetchall()
                for r in rows:
                    valid_ids.add(r["entity_id"])
        finally:
            conn.close()

        return [e for e in entities if e["id"] in valid_ids]

    def _save_corpus(
        self,
        name: str,
        project_id: str | None,
        types: list[str] | None,
        date_start: str | None,
        date_end: str | None,
        file_paths: list[str] | None,
        query: str | None,
        entity_count: int,
        token_count: int,
    ) -> str:
        from ulid import ULID

        corpus_id = str(ULID())
        now = datetime.now(UTC).isoformat()
        filters = json.dumps({
            "types": types,
            "date_start": date_start,
            "date_end": date_end,
            "file_paths": file_paths,
            "query": query,
        })

        conn = self.db.connect()
        try:
            conn.execute(
                "DELETE FROM corpora WHERE name = ?",
                (name,),
            )
            conn.execute(
                "INSERT INTO corpora "
                "(id, name, project_id, filters, entity_count, "
                "token_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    corpus_id, name, project_id, filters,
                    entity_count, token_count, now, now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return corpus_id

    def _save_corpus_entities(
        self,
        corpus_id: str,
        entities: list[dict[str, Any]],
    ) -> None:
        conn = self.db.connect()
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO corpus_entities "
                "(corpus_id, entity_id) VALUES (?, ?)",
                [(corpus_id, e["id"]) for e in entities],
            )
            conn.commit()
        finally:
            conn.close()

    def _get_corpus_by_name(
        self, name: str
    ) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM corpora WHERE name = ?", (name,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _load_corpus_entities(
        self, corpus_name: str
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT e.* FROM entities e "
                "JOIN corpus_entities ce ON e.id = ce.entity_id "
                "JOIN corpora c ON ce.corpus_id = c.id "
                "WHERE c.name = ? "
                "ORDER BY e.created_at DESC",
                (corpus_name,),
            ).fetchall()
            return [dict(Entity.from_row(dict(r)).to_row()) for r in rows]
        finally:
            conn.close()

    def _format_corpus_context(
        self, entities: list[dict[str, Any]]
    ) -> str:
        parts: list[str] = []
        for e in entities:
            eid = e.get("id", "")[:8]
            etype = e.get("type", "")
            title = e.get("title", "")
            content = e.get("content", "")
            parts.append(
                f"#{eid} [{etype}] {title}\n{content}\n"
            )
        return "\n".join(parts)
