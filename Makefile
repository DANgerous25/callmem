.PHONY: test dev setup lint typecheck session-save session-load clean

# Run the full test suite
test:
	uv run pytest tests/ -v

# Install all dependencies including dev extras
dev:
	uv sync --extra dev

# Interactive setup wizard
setup:
	uv run python scripts/setup.py

# Save session memory from recent git history
session-save:
	uv run python scripts/session_save.py --from-git
	@echo ""
	@echo "Memory files updated. Don't forget to commit:"
	@echo "  git add .llm-mem/ && git commit -m 'chore: update session memory' && git push"

# Load and display current session memory
session-load:
	uv run python scripts/session_load.py

# Run ruff linter
lint:
	uv run ruff check src/ tests/

# Run mypy type checks
typecheck:
	uv run mypy src/

# Remove build artifacts
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache __pycache__ dist/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
