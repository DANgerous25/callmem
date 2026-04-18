"""Integration tests for sensitive data detection in the ingest pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_mem.core.engine import MemoryEngine
from llm_mem.models.config import Config

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    pass


def _make_engine(memory_db: Database) -> MemoryEngine:
    return MemoryEngine(memory_db, Config())


class TestIngestWithRedaction:
    def test_sensitive_content_redacted(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        event = engine.ingest_one(
            "note", "AWS key is AKIAIOSFODNN7EXAMPLE for production"
        )
        assert event is not None
        assert "AKIA" not in event.content
        assert "[REDACTED:" in event.content

    def test_normal_content_unchanged(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "Just a normal coding note")
        assert event is not None
        assert event.content == "Just a normal coding note"

    def test_scan_status_in_metadata(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "AWS key AKIAIOSFODNN7EXAMPLE")
        assert event is not None
        assert event.metadata is not None
        assert "scan_status" in event.metadata

    def test_vault_entries_created(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one("note", "key = AKIAIOSFODNN7EXAMPLE")
        conn = memory_db.connect()
        try:
            rows = conn.execute("SELECT * FROM vault").fetchall()
            assert len(rows) >= 1
            assert rows[0]["category"] == "secret"
            assert rows[0]["ciphertext"] is not None
        finally:
            conn.close()

    def test_vault_entries_encrypt_original(self, memory_db: Database) -> None:
        from llm_mem.core.crypto import VaultKeyManager

        engine = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one("note", "key = AKIAIOSFODNN7EXAMPLE")
        conn = memory_db.connect()
        try:
            row = conn.execute("SELECT * FROM vault LIMIT 1").fetchone()
            vault_dir = memory_db.db_path.parent
            crypto = VaultKeyManager(vault_dir)
            decrypted = crypto.decrypt(row["ciphertext"])
            assert decrypted == "AKIAIOSFODNN7EXAMPLE"
        finally:
            conn.close()

    def test_redaction_disabled_in_config(self, memory_db: Database) -> None:
        config = Config(sensitive_data={"enabled": False})
        engine = MemoryEngine(memory_db, config)
        engine.start_session()
        event = engine.ingest_one("note", "key = AKIAIOSFODNN7EXAMPLE")
        assert event is not None
        assert "AKIA" in event.content

    def test_fts5_uses_redacted_content(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one(
            "note",
            "The database URL is postgres://admin:secret@db.example.com/mydb",
        )
        conn = memory_db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events_fts WHERE events_fts MATCH 'secret'"
            ).fetchall()
            assert len(rows) == 0  # "secret" was redacted
        finally:
            conn.close()

    def test_multiple_secrets_in_one_event(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        event = engine.ingest_one(
            "note",
            "AWS AKIAIOSFODNN7EXAMPLE and GitHub ghp_ABCDEFghijklmnopqrstuvwxyz1234567890",
        )
        assert event is not None
        assert "AKIA" not in event.content
        assert "ghp_" not in event.content
        assert event.content.count("[REDACTED:") >= 2

    def test_false_positive_unredacts_event(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "key = AKIAIOSFODNN7EXAMPLE")
        assert event is not None
        assert "AKIA" not in event.content

        conn = memory_db.connect()
        try:
            vault_row = conn.execute(
                "SELECT * FROM vault LIMIT 1"
            ).fetchone()
            vault_id = vault_row["id"]
        finally:
            conn.close()

        result = engine.mark_false_positive(vault_id)
        assert result["false_positive"] == 1

        refreshed = engine.get_event(event.id)
        assert refreshed is not None
        assert "AKIAIOSFODNN7EXAMPLE" in refreshed.content

    def test_false_positive_nonexistent_vault_raises(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        import pytest

        with pytest.raises(ValueError, match="Vault entry not found"):
            engine.mark_false_positive("nonexistent_id")

    def test_false_positive_idempotent(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "key = AKIAIOSFODNN7EXAMPLE")
        assert event is not None

        conn = memory_db.connect()
        try:
            vault_row = conn.execute("SELECT * FROM vault LIMIT 1").fetchone()
            vault_id = vault_row["id"]
        finally:
            conn.close()

        engine.mark_false_positive(vault_id)
        result = engine.mark_false_positive(vault_id)
        assert result["false_positive"] == 1

    def test_false_positive_restores_correct_value(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        event = engine.ingest_one(
            "note",
            "aws AKIAIOSFODNN7EXAMPLE and github ghp_ABCDEFghijklmnopqrstuvwxyz1234567890",
        )
        assert event is not None
        assert event.content.count("[REDACTED:") >= 2

        conn = memory_db.connect()
        try:
            rows = conn.execute("SELECT * FROM vault").fetchall()
            aws_vault_id = None
            for r in rows:
                placeholder = f"[REDACTED:secret:{r['id']}]"
                if placeholder in event.content:
                    aws_vault_id = r["id"]
                    break
            assert aws_vault_id is not None
        finally:
            conn.close()

        engine.mark_false_positive(aws_vault_id)

        refreshed = engine.get_event(event.id)
        assert refreshed is not None
        assert "AKIAIOSFODNN7EXAMPLE" in refreshed.content
        assert "ghp_" not in refreshed.content
