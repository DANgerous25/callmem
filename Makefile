PROJECT := $(shell pwd)
SVC_NAME := llm-mem-$(shell basename $(PROJECT))

.PHONY: test dev setup lint typecheck session-save session-load clean start stop restart logs status daemon watch

# Run the full test suite
test:
	uv run pytest tests/ -v

# Install all dependencies including dev extras
dev:
	uv sync --extra dev

# Interactive setup wizard
setup:
	uv run python scripts/setup.py

# Start the daemon (UI + workers + adapter) in foreground
daemon:
	uv run llm-mem daemon --project $(PROJECT)

# Start the systemd user service
start:
	systemctl --user start $(SVC_NAME)
	@echo "$(SVC_NAME) started. UI at http://$$(grep '^host' .llm-mem/config.toml 2>/dev/null | head -1 | cut -d'"' -f2 || echo 0.0.0.0):$$(grep '^port' .llm-mem/config.toml 2>/dev/null | head -1 | tr -d ' ' | cut -d= -f2 || echo 9090)"

# Stop the systemd user service
stop:
	systemctl --user stop $(SVC_NAME)
	@echo "$(SVC_NAME) stopped."

# Restart the systemd user service
restart:
	systemctl --user restart $(SVC_NAME)
	@echo "$(SVC_NAME) restarted."

# Follow daemon logs
logs:
	journalctl --user -u $(SVC_NAME) -f

# Save session memory from recent git history
session-save:
	uv run python scripts/session_save.py --from-git
	@echo ""
	@echo "Memory files updated. Don't forget to commit:"
	@echo "  git add .llm-mem/ && git commit -m 'chore: update session memory' && git push"

# Load and display current session memory
session-load:
	uv run python scripts/session_load.py

# Watch job queue progress
watch:
	python3 scripts/job_watch.py --project .

# Show memory status
status:
	uv run llm-mem status --project $(PROJECT)

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
