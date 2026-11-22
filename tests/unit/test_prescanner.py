"""Tests for the dedicated sensitive-data pre-scanner and its hold queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.engine import MemoryEngine, _create_scanner_client
from callmem.core.redaction import Detection
from callmem.models.config import Config

if TYPE_CHECKING:
    import pytest

    from callmem.core.database import Database

AWS = "AKIAIOSFODNN7EXAMPLE"
PROSE_SECRET = "hunter2hunter2"


class FakeScanner:
    """Stand-in pre-scanner. ``up`` toggles whether scans succeed."""

    def __init__(self, up: bool = True, flag: str | None = PROSE_SECRET) -> None:
        self.up = up
        self.flag = flag
        self.seen: list[str] = []

    def scan_sensitive_strict(self, content: str) -> list[Detection] | None:
        self.seen.append(content)
        if not self.up:
            return None
        if self.flag is None or self.flag not in content:
            return []
        start = content.index(self.flag)
        return [
            Detection(
                vault_id=f"01PRESCAN{len(self.seen):017d}",
                category="credential",
                detector="llm",
                pattern_name=None,
                original_value=self.flag,
                start=start,
                end=start + len(self.flag),
                confidence=0.9,
            )
        ]


class ForbiddenMainClient:
    """Main (remote) backend that must never be asked to scan."""

    def is_available(self) -> bool:
        return True

    def scan_sensitive(self, content: str) -> list[Detection]:
        raise AssertionError("remote backend was asked to scan")


def _engine(memory_db: Database, scanner: FakeScanner) -> MemoryEngine:
    config = Config(
        sensitive_data={
            "llm_scan": True,
            "scanner": {"backend": "openai_compat", "endpoint": "http://x/v1"},
        }
    )
    engine = MemoryEngine(memory_db, config)
    engine.llm_client = ForbiddenMainClient()
    engine.scanner_client = scanner
    engine.start_session()
    return engine


def _count(memory_db: Database, sql: str) -> int:
    conn = memory_db.connect()
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def _extract_jobs(memory_db: Database) -> int:
    return _count(memory_db, "SELECT COUNT(*) FROM jobs WHERE type = 'extract_entities'")


class TestScannerConfig:
    def test_default_is_not_set(self) -> None:
        assert Config().sensitive_data.scanner.backend == "none"

    def test_no_client_when_not_set(self) -> None:
        assert _create_scanner_client(Config()) is None

    def test_does_not_borrow_main_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_KEY", "not-for-the-scanner")
        config = Config(
            sensitive_data={
                "scanner": {"backend": "openai_compat", "endpoint": "http://x/v1"}
            }
        )
        client = _create_scanner_client(config)
        assert client is not None
        assert client.api_key == ""


class TestScannerUp:
    def test_redacts_and_never_uses_remote(self, memory_db: Database) -> None:
        scanner = FakeScanner()
        engine = _engine(memory_db, scanner)
        event = engine.ingest_one("note", f"key {AWS} and password {PROSE_SECRET} ok")
        assert event is not None
        assert AWS not in event.content
        assert PROSE_SECRET not in event.content
        assert all(AWS not in s for s in scanner.seen)
        assert event.metadata["scan_status"] == "full"
        assert _extract_jobs(memory_db) == 1


class TestScannerDown:
    def test_event_is_held_not_stored(self, memory_db: Database) -> None:
        engine = _engine(memory_db, FakeScanner(up=False))
        event = engine.ingest_one("note", f"key {AWS} password {PROSE_SECRET}")
        assert event is None
        assert _count(memory_db, "SELECT COUNT(*) FROM events") == 0
        assert _count(memory_db, "SELECT COUNT(*) FROM held_events") == 1
        assert _extract_jobs(memory_db) == 0
        assert engine.held_event_count() == 1

    def test_held_copy_is_pattern_redacted(self, memory_db: Database) -> None:
        engine = _engine(memory_db, FakeScanner(up=False))
        engine.ingest_one("note", f"key {AWS}")
        conn = memory_db.connect()
        try:
            payload = conn.execute("SELECT event_json FROM held_events").fetchone()[0]
        finally:
            conn.close()
        assert AWS not in payload

    def test_breaker_skips_scanner_during_cooldown(self, memory_db: Database) -> None:
        scanner = FakeScanner(up=False)
        engine = _engine(memory_db, scanner)
        engine.ingest_one("note", "first")
        engine.ingest_one("note", "second")
        assert len(scanner.seen) == 1
        assert engine.held_event_count() == 2


class TestRelease:
    def test_release_after_recovery(self, memory_db: Database) -> None:
        scanner = FakeScanner(up=False)
        engine = _engine(memory_db, scanner)
        engine.ingest_one("note", f"key {AWS} password {PROSE_SECRET} ok")
        scanner.up = True
        engine._scanner_down_until = 0.0

        released = engine.release_held_events()

        assert released == 1
        assert engine.held_event_count() == 0
        conn = memory_db.connect()
        try:
            content, metadata = conn.execute(
                "SELECT content, metadata FROM events"
            ).fetchone()
            detectors = sorted(r[0] for r in conn.execute("SELECT detector FROM vault"))
            count = conn.execute("SELECT event_count FROM sessions").fetchone()[0]
        finally:
            conn.close()
        assert AWS not in content
        assert PROSE_SECRET not in content
        assert content.endswith(" ok")
        assert '"scan_status": "full"' in metadata
        assert detectors == ["llm", "pattern"]
        assert count == 1
        assert _extract_jobs(memory_db) == 1

    def test_release_stops_on_failure_and_counts_attempt(
        self, memory_db: Database
    ) -> None:
        scanner = FakeScanner(up=False)
        engine = _engine(memory_db, scanner)
        engine.ingest_one("note", "one")
        engine._scanner_down_until = 0.0
        engine.ingest_one("note", "two")
        engine._scanner_down_until = 0.0

        assert engine.release_held_events() == 0
        assert engine.held_event_count() == 2
        # "one" was tried by the drain in the second ingest and again here;
        # the loop stopped there, so "two" was never tried.
        assert _count(memory_db, "SELECT MAX(attempts) FROM held_events") == 2
        assert _count(memory_db, "SELECT MIN(attempts) FROM held_events") == 0

    def test_release_is_exactly_once(self, memory_db: Database) -> None:
        scanner = FakeScanner(up=False)
        engine = _engine(memory_db, scanner)
        engine.ingest_one("note", "only once")
        scanner.up = True
        engine._scanner_down_until = 0.0
        row = engine.repo.list_held_events(10)[0]

        assert engine._release_one(row) is not None
        assert engine._release_one(row) is None
        assert _count(memory_db, "SELECT COUNT(*) FROM events") == 1

    def test_held_decision_becomes_entity(self, memory_db: Database) -> None:
        scanner = FakeScanner(up=False)
        engine = _engine(memory_db, scanner)
        engine.ingest_one("decision", "Use llama.cpp as the pre-scanner")
        scanner.up = True
        engine._scanner_down_until = 0.0
        engine.release_held_events()
        assert _count(memory_db, "SELECT COUNT(*) FROM entities WHERE type='decision'") == 1

    def test_ingest_drains_backlog_first(self, memory_db: Database) -> None:
        scanner = FakeScanner(up=False)
        engine = _engine(memory_db, scanner)
        engine.ingest_one("note", "held earlier")
        scanner.up = True
        engine._scanner_down_until = 0.0
        engine.ingest_one("note", "arrives later")
        assert engine.held_event_count() == 0
        assert _count(memory_db, "SELECT COUNT(*) FROM events") == 2

    def test_session_count_survives_drain_in_same_session(
        self, memory_db: Database
    ) -> None:
        scanner = FakeScanner(up=False)
        engine = _engine(memory_db, scanner)
        engine.ingest_one("note", "held earlier")
        scanner.up = True
        engine._scanner_down_until = 0.0
        engine.ingest_one("note", "arrives later")
        assert _count(memory_db, "SELECT event_count FROM sessions") == 2


class TestWizardPrescanner:
    def _answers(self, monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
        it = iter(answers)
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))

    def test_llama_cpp_preset_has_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from callmem import setup_wizard

        monkeypatch.setattr(setup_wizard, "_list_openai_models", lambda _e: ["qwen"])
        self._answers(monkeypatch, ["2", "http://10.200.200.1:8085/v1", ""])
        sc = setup_wizard._ask_prescanner({}, "http://localhost:11434")
        assert sc == {
            "backend": "openai_compat", "endpoint": "http://10.200.200.1:8085/v1",
            "model": "qwen", "api_key_env": "", "timeout": 60,
        }

    def test_default_is_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from callmem import setup_wizard

        self._answers(monkeypatch, [""])
        assert setup_wizard._ask_prescanner({}, "")["backend"] == "none"
        assert setup_wizard._render_scanner({"backend": "none"}) == ""

    def test_rendered_block_round_trips(self) -> None:
        import tomllib

        from callmem import setup_wizard

        block = setup_wizard._render_scanner({
            "backend": "openai_compat", "endpoint": "http://h:8085/v1",
            "model": "m", "api_key_env": "", "timeout": 60,
        })
        parsed = tomllib.loads("[sensitive_data]\nenabled = true\n" + block)
        config = Config(sensitive_data=parsed["sensitive_data"])
        assert config.sensitive_data.scanner.endpoint == "http://h:8085/v1"

    def test_remote_endpoint_is_flagged(self) -> None:
        from callmem import setup_wizard

        assert setup_wizard._is_local_endpoint("http://10.200.200.1:8085/v1")
        assert setup_wizard._is_local_endpoint("http://localhost:8080/v1")
        assert not setup_wizard._is_local_endpoint("https://openrouter.ai/api/v1")


class TestNewProjectInheritsScanner:
    def test_donor_scanner_is_copied(self) -> None:
        import tomllib

        from callmem.cli import _render_config_toml

        donor = {"sensitive_data": {"llm_scan": True, "scanner": {
            "backend": "openai_compat", "endpoint": "http://10.200.200.1:8085/v1",
            "model": "qwen3.6-35b-a3b", "api_key_env": "", "timeout": 60,
        }}}
        parsed = tomllib.loads(_render_config_toml("p", 9106, donor=donor))
        config = Config(**{k: parsed[k] for k in ("sensitive_data",)})
        assert config.sensitive_data.scanner.endpoint == "http://10.200.200.1:8085/v1"

    def test_no_donor_scanner_stays_unset(self) -> None:
        import tomllib

        from callmem.cli import _render_config_toml

        parsed = tomllib.loads(_render_config_toml("p", 9106))
        assert "scanner" not in parsed["sensitive_data"]


class TestKeylessMainBackend:
    def test_blank_key_env_never_borrows_openrouter_key(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from callmem.core.engine import _create_llm_client

        monkeypatch.setenv("OPENROUTER_KEY", "not-for-llama")
        config = Config(
            llm={"backend": "openai_compat"},
            openai_compat={"endpoint": "http://10.0.0.5:8085/v1", "api_key_env": ""},
        )
        assert _create_llm_client(config).api_key == ""
