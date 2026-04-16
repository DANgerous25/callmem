# WO-03: CLI Skeleton and Configuration Loading

## Objective

Create the CLI entry point with `click`, implement configuration loading (TOML files + env vars + CLI flags), and wire up the `init` and `serve` commands as stubs.

## Files to create

- `src/llm_mem/cli.py` — Click CLI group and commands
- `src/llm_mem/core/config.py` — Configuration loading, merging, validation
- `src/llm_mem/models/config.py` — Pydantic config model (typed config schema)
- `tests/unit/test_config.py`
- `tests/unit/test_cli.py`

## Files to modify

- `pyproject.toml` — Add `[project.scripts]` entry point: `llm-mem = "llm_mem.cli:main"`

## Constraints

- Config loading order: defaults → global TOML → project TOML → env vars → CLI flags
- Use `tomllib` (Python 3.11+) for TOML parsing
- Env vars use `LLM_MEM_` prefix with `__` separator for nesting
- CLI should use `click` (not `typer` — fewer dependencies, more control)
- Config validation on load — warn on unknown keys, error on invalid values
- All commands should accept `--project` flag for project root

## Commands to implement

```
llm-mem init [--project PATH]
    - Creates .llm-mem/ directory
    - Creates .llm-mem/config.toml with defaults
    - Initializes SQLite database
    - Prints confirmation

llm-mem serve [--project PATH] [--transport stdio|sse] [--no-workers]
    - Loads config
    - Initializes database
    - Prints "MCP server ready" (actual MCP server is WO-05)
    - Stub: exits cleanly

llm-mem ui [--project PATH] [--port PORT]
    - Loads config
    - Prints "UI server would start on http://127.0.0.1:{port}"
    - Stub: exits cleanly

llm-mem status [--project PATH]
    - Shows: database path, size, event count, session count, last session date
```

## Acceptance criteria

1. `llm-mem --help` shows available commands
2. `llm-mem init --project /tmp/test-project` creates `.llm-mem/` directory with `config.toml` and `memory.db`
3. `llm-mem status --project /tmp/test-project` shows database stats
4. Config merging works: project config overrides global, env vars override both
5. Invalid config values produce clear error messages
6. `pytest tests/unit/test_config.py` and `tests/unit/test_cli.py` pass

## Suggested tests

```python
def test_config_defaults():
    config = load_config()  # No files, no env vars
    assert config.ollama.model == "qwen3:8b"
    assert config.briefing.max_tokens == 2000
    assert config.ui.port == 9090

def test_config_toml_override(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[ollama]\nmodel = "llama3.1:8b"\n')
    config = load_config(project_path=tmp_path)
    assert config.ollama.model == "llama3.1:8b"

def test_config_env_override(monkeypatch):
    monkeypatch.setenv("LLM_MEM_OLLAMA__MODEL", "gemma2:9b")
    config = load_config()
    assert config.ollama.model == "gemma2:9b"

def test_init_creates_directory(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / ".llm-mem" / "memory.db").exists()
    assert (tmp_path / ".llm-mem" / "config.toml").exists()
```
