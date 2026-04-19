# WO-01: Project Setup and Database Initialization

## Objective

Set up the Python project with `pyproject.toml`, install dependencies, create the database module with schema initialization, and migration runner.

## Files to create

- `pyproject.toml` — Project metadata and dependencies
- `src/callmem/__init__.py` — Package init with version
- `src/callmem/core/__init__.py`
- `src/callmem/core/database.py` — Database connection manager, schema init, migration runner
- `src/callmem/core/migrations/001_initial.sql` — Full initial schema DDL
- `tests/__init__.py`
- `tests/conftest.py` — Shared fixtures (temp database, etc.)
- `tests/unit/__init__.py`
- `tests/unit/test_database.py` — Tests for database init and migrations
- `.gitignore`

## Constraints

- Use `uv` as package manager (with `pyproject.toml` — no `setup.py` or `setup.cfg`)
- Python 3.11+ required
- Dependencies for v0: `pydantic>=2.0`, `python-ulid`, `click`, `tomli` (for <3.11 compat) / `tomllib`
- Dev dependencies: `pytest`, `pytest-asyncio`, `ruff`, `mypy`
- Database module must use context managers for connections
- All SQL in the migration file, not inline in Python
- WAL mode enabled by default
- Use `pathlib.Path` throughout, not string paths

## Acceptance criteria

1. `uv sync` installs all dependencies successfully
2. `python -c "from callmem.core.database import Database; db = Database(':memory:'); db.initialize()"` creates all tables
3. Schema version table shows version 1
4. All FTS5 virtual tables are created
5. All triggers are in place
6. `pytest tests/unit/test_database.py` passes with tests for:
   - Database creation from scratch
   - Schema version tracking
   - Idempotent initialization (running init twice doesn't error)
   - WAL mode is enabled
   - All expected tables exist

## Suggested tests

```python
def test_database_creates_all_tables(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    tables = db.list_tables()
    assert "projects" in tables
    assert "sessions" in tables
    assert "events" in tables
    assert "entities" in tables
    assert "summaries" in tables
    assert "memory_edges" in tables
    assert "compaction_log" in tables
    assert "config" in tables
    assert "schema_version" in tables

def test_fts5_tables_created(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    tables = db.list_tables()
    assert "events_fts" in tables
    assert "entities_fts" in tables
    assert "summaries_fts" in tables

def test_schema_version_set(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    version = db.get_schema_version()
    assert version == 1

def test_wal_mode_enabled(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"

def test_idempotent_init(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.initialize()  # Should not raise
    assert db.get_schema_version() == 1
```
