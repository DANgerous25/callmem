# Contributing to callmem

Thanks for your interest in contributing. This document covers how to get started, what we expect, and how to submit changes.

## Getting Started

```bash
git clone https://github.com/DANgerous25/callmem.git
cd callmem
uv sync --extra dev
```

Ensure you have Ollama running with a model for integration tests:

```bash
ollama pull qwen3:8b
```

## Development Workflow

```bash
# Run tests
make test

# Run linter
make lint

# Run both
make check

# Start the daemon locally
make daemon
```

## Code Standards

- **Python 3.11+** required
- **Ruff** for linting and formatting — run `make lint` before committing
- **Type hints** on all public functions
- **Tests** for all new functionality — we have 382+ tests and expect coverage to grow
- Keep imports clean: use `TYPE_CHECKING` blocks for annotation-only imports (ruff TCH rules)

## Commit Messages

Use conventional commit style:

```
feat: add file-level observation tracking
fix: prevent duplicate entities on re-import
docs: update configuration reference
test: add integration tests for SSE endpoint
refactor: extract briefing formatting into separate module
```

For work-order implementations, prefix with the WO number:

```
feat(WO-16): file tracking and progressive disclosure search
```

## Pull Requests

1. Fork the repo and create a feature branch
2. Make your changes with tests
3. Run `make check` to ensure lint + tests pass
4. Open a PR with a clear description of what changed and why

## Architecture

Read [docs/architecture.md](docs/architecture.md) for an overview of the system components. Key files:

| Area | Location |
|---|---|
| Core engine | `src/callmem/core/engine.py` |
| Entity extraction | `src/callmem/core/extraction.py` |
| Briefing generation | `src/callmem/core/briefing.py` |
| LLM prompts | `src/callmem/core/prompts.py` |
| Data models | `src/callmem/models/` |
| Web UI routes | `src/callmem/ui/routes/` |
| MCP server | `src/callmem/mcp/server.py` |
| OpenCode adapter | `src/callmem/adapters/opencode.py` |

## Work Orders

Implementation tasks are tracked as work orders in `docs/`. If you want to tackle one, check the open GitHub issues.

## Reporting Issues

Open an issue on GitHub with:
- What you were trying to do
- What happened
- What you expected to happen
- Your environment (OS, Python version, Ollama model)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
