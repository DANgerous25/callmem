"""Tests for re-scanning already-stored memory for secrets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from callmem.core.redaction import Detection
from callmem.core.rescan import Rescanner, ScannerUnavailableError
from callmem.models.config import Config
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine
    from callmem.core.rescan import Restorer

AWS = "AKIAIOSFODNN7EXAMPLE"
PROSE = "dolphin42staging"


class FakeScanner:
    """Flags ``flag`` wherever it appears; ``up=False`` simulates an outage."""

    def __init__(self, flag: str | None = PROSE, up: bool = True) -> None:
        self.flag = flag
        self.up = up
        self.calls: list[str] = []

    def scan_sensitive_strict(self, content: str) -> list[Detection] | None:
        self.calls.append(content)
        if not self.up:
            return None
        if not self.flag or self.flag not in content:
            return []
        start = content.index(self.flag)
        return [Detection(
            vault_id="x", category="credential", detector="llm", pattern_name=None,
            original_value=self.flag, start=start, end=start + len(self.flag),
            confidence=0.9,
        )]


def _engine(memory_db: Database) -> MemoryEngine:
    from callmem.core.engine import MemoryEngine

    # Rows are inserted raw below, as they were before redaction got stricter.
    engine = MemoryEngine(memory_db, Config(sensitive_data={"enabled": False}))
    engine.start_session()
    return engine


def _raw_event(memory_db: Database, engine: MemoryEngine, content: str) -> str:
    event = engine.ingest_one("note", content)
    assert event is not None
    return event.id


def _entity(engine: MemoryEngine, title: str, content: str) -> Entity:
    entity = Entity(
        project_id=engine.get_active_session().project_id,
        type="fact", title=title, content=content,
    )
    engine.repo.create_entity(entity)
    return entity


def _rescanner(
    memory_db: Database, tmp_path: Path, scanner: FakeScanner, **kw: object
) -> Rescanner:
    from callmem.core.repository import Repository

    return Rescanner(
        Repository(memory_db), scanner, Config(), vault_dir=tmp_path,
        progress_path=tmp_path / "rescan_progress.json", **kw,
    )


def _one(memory_db: Database, sql: str, *args: object) -> object:
    conn = memory_db.connect()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


class TestRescan:
    def test_redacts_events_and_entities(self, memory_db: Database, tmp_path: Path) -> None:
        engine = _engine(memory_db)
        ev = _raw_event(memory_db, engine, f"key {AWS} and the password is {PROSE} ok")
        ent = _entity(engine, f"Staging password {PROSE}", f"AWS key {AWS}")

        stats = _rescanner(memory_db, tmp_path, FakeScanner()).run()

        content = _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)
        assert AWS not in content and PROSE not in content and content.endswith(" ok")
        title, body = (
            _one(memory_db, "SELECT title FROM entities WHERE id = ?", ent.id),
            _one(memory_db, "SELECT content FROM entities WHERE id = ?", ent.id),
        )
        assert PROSE not in title and AWS not in body
        assert _one(memory_db, "SELECT COUNT(*) FROM vault") == 4
        assert stats.rows_changed["events.content"] == 1
        assert set(stats.changed_entities) == {ent.id}

    def test_fts_index_no_longer_matches(self, memory_db: Database, tmp_path: Path) -> None:
        engine = _engine(memory_db)
        _entity(engine, "creds", f"the password is {PROSE}")
        _rescanner(memory_db, tmp_path, FakeScanner()).run()
        hits = _one(
            memory_db, "SELECT COUNT(*) FROM entities_fts WHERE entities_fts MATCH ?", PROSE,
        )
        assert hits == 0

    def test_values_found_in_events_propagate(
        self, memory_db: Database, tmp_path: Path
    ) -> None:
        engine = _engine(memory_db)
        _raw_event(memory_db, engine, f"MARK password {PROSE}")
        ent = _entity(engine, "note", f"later reused {PROSE}")

        class OnlyMarked(FakeScanner):
            def scan_sensitive_strict(self, content: str) -> list[Detection] | None:
                return super().scan_sensitive_strict(content) if "MARK" in content else []

        _rescanner(memory_db, tmp_path, OnlyMarked()).run()
        assert PROSE not in _one(memory_db, "SELECT content FROM entities WHERE id = ?", ent.id)

    def test_scanner_down_stops_without_writing(
        self, memory_db: Database, tmp_path: Path
    ) -> None:
        engine = _engine(memory_db)
        ev = _raw_event(memory_db, engine, f"the password is {PROSE}")
        scanner = FakeScanner(up=False)
        try:
            _rescanner(memory_db, tmp_path, scanner).run()
            raise AssertionError("expected ScannerUnavailableError")
        except ScannerUnavailableError:
            pass
        assert PROSE in _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)

        scanner.up = True
        _rescanner(memory_db, tmp_path, scanner).run()
        assert PROSE not in _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)

    def test_resume_skips_done_rows(self, memory_db: Database, tmp_path: Path) -> None:
        engine = _engine(memory_db)
        _raw_event(memory_db, engine, "harmless")
        scanner = FakeScanner()
        _rescanner(memory_db, tmp_path, scanner).run()
        first = len(scanner.calls)
        _rescanner(memory_db, tmp_path, scanner).run()
        assert len(scanner.calls) == first

    def test_dry_run_writes_nothing(self, memory_db: Database, tmp_path: Path) -> None:
        engine = _engine(memory_db)
        ev = _raw_event(memory_db, engine, f"key {AWS}")
        stats = _rescanner(memory_db, tmp_path, FakeScanner(), dry_run=True).run()
        assert AWS in _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)
        assert _one(memory_db, "SELECT COUNT(*) FROM vault") == 0
        assert not (tmp_path / "rescan_progress.json").exists()
        assert stats.rows_changed["events.content"] == 1

    def test_long_text_is_chunked(self, memory_db: Database, tmp_path: Path) -> None:
        engine = _engine(memory_db)
        ev = _raw_event(memory_db, engine, "x " * 6000 + f"password {PROSE}")
        scanner = FakeScanner()
        _rescanner(memory_db, tmp_path, scanner, batch_chars=2000).run()
        assert all(len(c) <= 2600 for c in scanner.calls)
        assert PROSE not in _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)

    def test_short_llm_values_ignored(self, memory_db: Database, tmp_path: Path) -> None:
        engine = _engine(memory_db)
        ev = _raw_event(memory_db, engine, "user admin logged in")
        _rescanner(memory_db, tmp_path, FakeScanner(flag="admin")).run()
        assert _one(memory_db, "SELECT content FROM events WHERE id = ?", ev) == (
            "user admin logged in"
        )

    def test_concurrent_edit_is_not_clobbered(
        self, memory_db: Database, tmp_path: Path
    ) -> None:
        from callmem.core.repository import Repository

        engine = _engine(memory_db)
        ent = _entity(engine, "t", f"key {AWS}")
        repo = Repository(memory_db)
        rowid = _one(memory_db, "SELECT rowid FROM entities WHERE id = ?", ent.id)
        conn = memory_db.connect()
        conn.execute("UPDATE entities SET content = 'edited' WHERE id = ?", (ent.id,))
        conn.commit()
        conn.close()
        assert not repo.update_text_if_unchanged(
            "entities", "content", rowid, f"key {AWS}", "redacted",
        )
        assert _one(memory_db, "SELECT content FROM entities WHERE id = ?", ent.id) == "edited"


class TestRescanCli:
    def test_refuses_without_local_scanner(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from callmem.cli import main
        from callmem.core.database import Database

        (tmp_path / ".callmem").mkdir()
        Database(tmp_path / ".callmem" / "memory.db").initialize()
        result = CliRunner().invoke(main, ["rescan", "--project", str(tmp_path)])
        assert result.exit_code == 1
        assert "No pre-scanner set" in result.output


class TestPlausibleFinding:
    def test_identifiers_and_paths_are_not_secrets(self) -> None:
        from callmem.core.redaction import plausible_llm_finding

        assert not plausible_llm_finding("01M81QDC5RRXTKVV3MP6XK4M1D", "credential")
        assert not plausible_llm_finding("3f2b8c1e-9a4d-4e6f-b1c2-7d8e9f0a1b2c", "secret")
        assert not plausible_llm_finding("TURNSTILE_SECRET", "credential")
        assert not plausible_llm_finding("/home/dan/ratemyfur", "pii")
        assert not plausible_llm_finding("10.200.200.2", "infra")
        assert not plausible_llm_finding("admin", "credential")
        assert plausible_llm_finding("dolphin42staging", "credential")
        assert plausible_llm_finding("x9Kq2mP4vL8nR3tW6yB1cD5f", "secret")


class TruncatingScanner(FakeScanner):
    """Cuts its reply off (IncompleteScanError) for texts over ``max_len``."""

    def __init__(self, max_len: int, **kw: object) -> None:
        super().__init__(**kw)
        self.max_len = max_len

    def scan_sensitive_strict(self, content: str) -> list[Detection] | None:
        from callmem.core.redaction import IncompleteScanError

        if len(content) > self.max_len:
            self.calls.append(content)
            raise IncompleteScanError("reply cut off")
        return super().scan_sensitive_strict(content)


class TestScanInPieces:
    def test_splits_until_replies_fit(self) -> None:
        from callmem.core.redaction import scan_in_pieces

        text = "filler " * 300 + f"password {PROSE} " + "tail " * 300
        scanner = TruncatingScanner(max_len=800)
        hits = scan_in_pieces(scanner.scan_sensitive_strict, text, min_chars=200)
        assert hits is not None and len(hits) == 1
        h = hits[0]
        assert text[h.start:h.end] == PROSE

    def test_gives_up_below_min_chars(self) -> None:
        from callmem.core.redaction import scan_in_pieces

        scanner = TruncatingScanner(max_len=10)
        assert scan_in_pieces(scanner.scan_sensitive_strict, "x" * 500, min_chars=200) is None

    def test_no_answer_does_not_split(self) -> None:
        from callmem.core.redaction import scan_in_pieces

        scanner = FakeScanner(up=False)
        assert scan_in_pieces(scanner.scan_sensitive_strict, "x" * 5000) is None
        assert len(scanner.calls) == 1

    def test_rescan_survives_dense_batch(self, memory_db: Database, tmp_path: Path) -> None:
        engine = _engine(memory_db)
        ev = _raw_event(memory_db, engine, "note " * 200 + f"password {PROSE}")
        _rescanner(memory_db, tmp_path, TruncatingScanner(max_len=700)).run()
        assert PROSE not in _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)


def _keep(text: str, value: str, category: str = "credential") -> bool:
    from callmem.core.redaction import keep_llm_finding

    start = text.index(value)
    return keep_llm_finding(text, start, start + len(value), category)


class TestKeepInContext:
    def test_non_secrets_seen_in_the_wild_are_dropped(self) -> None:
        assert not _keep("chmod 750 scripts/nightly-backup.sh", "nightly")
        assert not _keep("cd /tmp/x/-home-dan-boatproj/abc", "home-dan-boatproj", "pii")
        assert not _keep("ssh -i ~/.ssh/id_ed25519_ha root@host", "id_ed25519_ha")
        assert not _keep("ssh -p 22224 hassio@192.168.1.132 'ls'", "hassio")
        assert not _keep("docker exec -i homeassistant python3", "homeassistant")
        assert not _keep("Available options: ['assistant pipeline', 'other']",
                         "assistant pipeline")
        assert not _keep('curl -H "Authorization: Bearer $TOKEN" x', "$TOKEN")
        assert not _keep("the endpoint is called using a access token.", "access token")
        assert not _keep("getent hosts boatbox.local", "boatbox.local", "pii")

    def test_real_secrets_are_kept(self) -> None:
        assert _keep("basic-auth with admin + harbourlights", "harbourlights")
        assert _keep("export DB_PASSWORD=dolphin42staging", "dolphin42staging")
        assert _keep("key x9Kq2mP4vL8nR3tW6yB1cD5f in a note", "x9Kq2mP4vL8nR3tW6yB1cD5f")
        assert _keep("recipients: - '+852 9123 4567'", "+852 9123 4567", "pii")
        assert _keep("contact Jane Doakes about it", "Jane Doakes", "pii")


class MultiScanner(FakeScanner):
    """Flags every value in ``flags`` that appears."""

    def __init__(self, flags: list[str]) -> None:
        super().__init__()
        self.flags = flags

    def scan_sensitive_strict(self, content: str) -> list[Detection] | None:
        hits = []
        for f in self.flags:
            if f in content:
                s = content.index(f)
                hits.append(Detection(
                    vault_id="x", category="credential", detector="llm",
                    pattern_name=None, original_value=f, start=s, end=s + len(f),
                    confidence=0.9,
                ))
        return hits


class TestRestore:
    def _over_redact(
        self, memory_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[str, Entity]:
        import callmem.core.rescan as rescan_mod

        engine = _engine(memory_db)
        ev = _raw_event(
            memory_db, engine,
            f"ran scripts/nightly-backup.sh then export DB_PASSWORD={PROSE}",
        )
        ent = _entity(engine, "backup", "the nightly job, see scripts/nightly-backup.sh")
        # Reproduce the old filter, which redacted anything plausible.
        monkeypatch.setattr(rescan_mod, "keep_llm_finding", lambda *a, **k: True)
        _rescanner(memory_db, tmp_path, MultiScanner(["nightly", PROSE])).run()
        monkeypatch.undo()
        assert "nightly" not in _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)
        return ev, ent

    def _restorer(self, memory_db: Database, tmp_path: Path, **kw: Any) -> Restorer:
        from callmem.core.repository import Repository
        from callmem.core.rescan import Restorer

        return Restorer(Repository(memory_db), tmp_path, since="2000-01-01", **kw)

    def test_restores_false_positives_keeps_secrets(
        self, memory_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ev, ent = self._over_redact(memory_db, tmp_path, monkeypatch)
        stats = self._restorer(memory_db, tmp_path).run()

        content = _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)
        assert "scripts/nightly-backup.sh" in content and PROSE not in content
        body = _one(memory_db, "SELECT content FROM entities WHERE id = ?", ent.id)
        assert body == "the nightly job, see scripts/nightly-backup.sh"
        assert stats.values_restored["credential"] == 1
        assert stats.values_kept["credential"] == 1
        assert set(stats.changed_entities) == {ent.id}
        flagged = _one(memory_db, "SELECT COUNT(*) FROM vault WHERE false_positive = 1")
        assert flagged == 3  # one per restored occurrence

    def test_dry_run_changes_nothing(
        self, memory_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ev, _ = self._over_redact(memory_db, tmp_path, monkeypatch)
        before = _one(memory_db, "SELECT content FROM events WHERE id = ?", ev)
        stats = self._restorer(memory_db, tmp_path, dry_run=True).run()
        assert _one(memory_db, "SELECT content FROM events WHERE id = ?", ev) == before
        assert stats.tokens_restored["events.content"] == 1
        assert _one(memory_db, "SELECT COUNT(*) FROM vault WHERE false_positive = 1") == 0

    def test_second_run_is_a_no_op(
        self, memory_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._over_redact(memory_db, tmp_path, monkeypatch)
        self._restorer(memory_db, tmp_path).run()
        stats = self._restorer(memory_db, tmp_path).run()
        assert not stats.tokens_restored


class TestRestoreRatio:
    def test_common_term_with_one_stray_context_is_restored(
        self, memory_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import callmem.core.rescan as rescan_mod
        from callmem.core.repository import Repository
        from callmem.core.rescan import Restorer

        engine = _engine(memory_db)
        term = "kioskboard"
        _raw_event(memory_db, engine, f"set the token to {term} by mistake")
        for i in range(25):
            _raw_event(memory_db, engine, f"open the {term} view {i}")
        monkeypatch.setattr(rescan_mod, "keep_llm_finding", lambda *a, **k: True)
        _rescanner(memory_db, tmp_path, MultiScanner([term])).run()
        monkeypatch.undo()

        Restorer(Repository(memory_db), tmp_path, since="2000-01-01").run()
        n = _one(memory_db, "SELECT COUNT(*) FROM events WHERE content LIKE ?", f"%{term}%")
        assert n == 26

    def test_credential_word_itself_is_not_a_secret(self) -> None:
        assert not _keep("basic-auth with admin + password", "password")
