# WO-17: Web UI Settings Panel with Live Briefing Preview

## Priority: P1

## Objective

Add a settings page to the web UI where users can view and edit callmem configuration, with a live preview showing exactly what will be injected into OpenCode's context at session start.

## Reference

claude-mem has a Context Settings Modal with:
- Sliders for observation count (1-200) and session count (1-50)
- Toggleable type filters (bugfix, feature, discovery, etc.)
- Full observations count (0-20) and field selector (narrative vs facts)
- Token economics toggles
- Model selector, port config
- **Live Terminal Preview** showing the exact text that will be injected

---

## 1. Settings Page

### Route: `/settings`

Add to the nav bar between "Search" and "Briefing".

### Settings to expose

Group settings into sections:

#### Context Injection
| Setting | Config key | Type | Default | Description |
|---------|-----------|------|---------|-------------|
| Max briefing tokens | `briefing.max_tokens` | slider 500-5000 | 2000 | Token budget for startup briefing |
| Entity types to include | `briefing.entity_types` | checkboxes | all | Which types appear in briefing |
| Max entities per type | `briefing.max_per_type` | number 1-50 | 20 | Cap per entity type |
| Include last session summary | `briefing.include_last_session` | toggle | true | Show previous session summary |
| Default content view | `briefing.default_view` | radio | key_points | "key_points" or "synopsis" in briefing |
| Auto-write SESSION_SUMMARY.md | `briefing.auto_write_session_summary` | toggle | true | Write to project root automatically |

#### LLM Backend
| Setting | Config key | Type | Default | Description |
|---------|-----------|------|---------|-------------|
| Backend | `llm.backend` | select | ollama | ollama / openai_compat / none |
| Model | `llm.model` | text | qwen3:8b | Model name |
| API base URL | `llm.api_base` | text | http://localhost:11434 | Ollama/API URL |

#### Server
| Setting | Config key | Type | Default | Description |
|---------|-----------|------|---------|-------------|
| Web UI port | `server.port` | number | 9090 | UI bind port |
| Bind address | `server.host` | text | 0.0.0.0 | Network bind |

#### Extraction
| Setting | Config key | Type | Default | Description |
|---------|-----------|------|---------|-------------|
| Batch size | `extraction.batch_size` | number 1-20 | 10 | Events per extraction batch |

### Implementation

Create `src/callmem/ui/routes/settings.py`:

- `GET /settings` — render settings form with current values from `config.toml`
- `POST /settings` — validate, update `config.toml`, reload config in memory
- Config file: read/write using `tomllib` (read) and `tomli_w` (write) or string manipulation
- Back up existing `config.toml` to `config.toml.bak` before writing (same pattern as setup wizard)

### Form layout
- Use Pico CSS form elements (inputs, selects, checkboxes, range sliders)
- Group into `<fieldset>` sections with `<legend>` headers
- Submit button at bottom: "Save Settings"
- Success/error flash message after save

---

## 2. Live Briefing Preview

### What it does

Below (or beside) the settings form, show a live preview of the briefing that would be generated with the current settings. This shows users exactly what OpenCode will see.

### Implementation

- Add a `<div id="briefing-preview">` section on the settings page
- On page load and after any setting change, call `/partials/briefing-preview` via htmx
- The endpoint generates a briefing using the current (or proposed) settings and returns formatted HTML
- Render in a `<pre class="terminal-preview">` block with monospace font, dark background, to simulate terminal output
- Debounce updates: 500ms after last setting change (don't regenerate on every keystroke)

### htmx integration

```html
<div id="briefing-preview"
     hx-get="/partials/briefing-preview"
     hx-trigger="load, settings-changed from:body delay:500ms"
     hx-swap="innerHTML">
  Loading preview...
</div>
```

Settings form fields emit a custom `settings-changed` event via JS on change.

For proposed (unsaved) settings, send them as query params:
```
/partials/briefing-preview?max_tokens=3000&entity_types=bugfix,feature,discovery
```

### Files to Create

- `src/callmem/ui/routes/settings.py` — settings page + POST handler + preview partial
- `src/callmem/ui/templates/settings.html` — form + preview layout

### Files to Modify

- `src/callmem/ui/app.py` — register settings router
- `src/callmem/ui/templates/base.html` — add "Settings" to nav, CSS for terminal preview
- `src/callmem/models/config.py` — add any missing config fields (briefing.entity_types, etc.)

---

## Acceptance Criteria

1. [ ] `/settings` page accessible from nav bar
2. [ ] All context injection settings are editable (max tokens, entity types, max per type, etc.)
3. [ ] LLM backend settings editable (backend, model, API base URL)
4. [ ] Server settings shown (port, host) — read-only or with restart warning
5. [ ] Settings saved to `config.toml` on submit, backup created first
6. [ ] Config reloaded in memory after save (no daemon restart needed for most settings)
7. [ ] Live briefing preview updates on setting change (500ms debounce)
8. [ ] Preview renders in terminal-style monospace block
9. [ ] Preview uses proposed (unsaved) settings, not just current ones
10. [ ] Success/error flash message after save
11. [ ] All existing tests pass, new tests for settings routes
12. [ ] `make lint` clean, `make test` all pass
13. [ ] Committed and pushed
