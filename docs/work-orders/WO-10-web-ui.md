# WO-10: Web UI

## Objective

Implement the local web UI using FastAPI + htmx + Pico CSS. The UI provides a browser-based interface for inspecting, searching, and managing memories.

## Files to create

- `src/llm_mem/ui/__init__.py`
- `src/llm_mem/ui/app.py` — FastAPI application factory
- `src/llm_mem/ui/routes/dashboard.py` — Dashboard page
- `src/llm_mem/ui/routes/sessions.py` — Sessions list and detail
- `src/llm_mem/ui/routes/search.py` — Memory search
- `src/llm_mem/ui/routes/entities.py` — Entity browser (TODOs, decisions, facts, etc.)
- `src/llm_mem/ui/routes/briefing.py` — Briefing preview
- `src/llm_mem/ui/routes/settings.py` — Settings page
- `src/llm_mem/ui/templates/base.html` — Base template with nav and Pico CSS
- `src/llm_mem/ui/templates/dashboard.html`
- `src/llm_mem/ui/templates/sessions.html`
- `src/llm_mem/ui/templates/session_detail.html`
- `src/llm_mem/ui/templates/search.html`
- `src/llm_mem/ui/templates/entities.html`
- `src/llm_mem/ui/templates/briefing.html`
- `src/llm_mem/ui/templates/settings.html`
- `src/llm_mem/ui/templates/partials/` — htmx partial templates
- `src/llm_mem/ui/static/htmx.min.js` — Vendored htmx
- `tests/integration/test_ui.py`

## Files to modify

- `src/llm_mem/cli.py` — Wire `llm-mem ui` to launch FastAPI with uvicorn
- `pyproject.toml` — Add `fastapi`, `uvicorn`, `jinja2` dependencies

## Constraints

- Use Pico CSS (classless) — minimal custom CSS
- Use htmx for interactivity — no custom JavaScript beyond htmx attributes
- All data access goes through `MemoryEngine` — no direct SQL in route handlers
- Vendor htmx.min.js and pico.min.css for offline use
- Templates use Jinja2
- UI binds to `127.0.0.1` by default (not `0.0.0.0`)
- The UI reads/writes the same SQLite database — WAL mode handles concurrency

## Pages to implement

### Dashboard (`/`)
- Project name, DB path, DB size
- Active session indicator
- Stats: total events, entities, sessions
- Last compaction date
- Quick action buttons: generate briefing, run compaction

### Sessions (`/sessions`)
- Paginated list of sessions (newest first)
- Each row: date, duration, event count, summary snippet, status badge
- Click to expand → session detail

### Session detail (`/sessions/{id}`)
- Session metadata (agent, model, duration)
- Session summary
- Extracted entities list
- Event timeline (paginated, load-more via htmx)

### Search (`/search`)
- Search input with auto-submit on enter
- Type filter checkboxes (decisions, TODOs, facts, etc.)
- Results list with type badges, snippets, dates, scores
- Pin/delete actions on each result (htmx swap)

### Entities (`/entities/{type}`)
- Filter by type: todos, decisions, facts, failures, discoveries
- For TODOs: group by status (open/done/cancelled)
- Pin/unpin toggle (htmx)
- Inline edit (htmx)
- Delete (soft, htmx)
- Link to source event

### Briefing (`/briefing`)
- Rendered briefing preview
- Token count display
- Regenerate button (htmx)
- Focus input for filtered briefing

### Settings (`/settings`)
- Form with all config options from config.md
- Save button
- Ollama connection test button
- Danger zone: reset memory, export DB

## Acceptance criteria

1. `llm-mem ui --project .` starts a web server on port 9090
2. Dashboard loads and shows project stats
3. Sessions list shows existing sessions
4. Session detail shows events and extracted entities
5. Search returns relevant results
6. Pin/unpin works via htmx without full page reload
7. Entity edit works via htmx
8. Briefing preview renders correctly
9. Settings form saves configuration
10. All pages render correctly with Pico CSS
11. `pytest tests/integration/test_ui.py` passes (test routes return 200)

## Suggested tests

```python
def test_dashboard_loads(test_client):
    response = test_client.get("/")
    assert response.status_code == 200
    assert "llm-mem" in response.text

def test_sessions_list(test_client_with_data):
    response = test_client_with_data.get("/sessions")
    assert response.status_code == 200

def test_search(test_client_with_data):
    response = test_client_with_data.get("/search?q=pagination")
    assert response.status_code == 200

def test_entities_todos(test_client_with_data):
    response = test_client_with_data.get("/entities/todos")
    assert response.status_code == 200

def test_pin_entity(test_client_with_data):
    entity_id = "..."  # From fixture
    response = test_client_with_data.post(f"/entities/{entity_id}/pin")
    assert response.status_code == 200
```
