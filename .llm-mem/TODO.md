# TODO — llm-mem

Track work order progress and ad-hoc tasks here.

## Work Order Progress

| WO | Title | Status |
|----|-------|--------|
| WO-01 | Project setup and database initialization | **Complete** |
| WO-02 | Data models and type system | **Complete** |
| WO-03 | CLI skeleton and configuration loading | **Complete** |
| WO-04 | Core engine — ingest and session management | **Complete** |
| WO-04b | Sensitive data detection and encrypted vault | **Complete** |
| WO-05 | MCP server with core tools | **Complete** |
| WO-06 | Ollama integration and entity extraction | **Complete** |
| WO-07 | Retrieval engine and startup briefing | **Complete** |
| WO-08 | Summarization workers | **Complete** |
| WO-09 | Memory compaction | **Complete** |
| WO-10 | Web UI | **Complete** |
| WO-11 | Background worker runner | **Complete** |
| WO-12 | OpenCode adapter and AGENTS.md template | **Complete** |
| WO-13 | Feed card UI improvements | **Complete** |
| WO-14 | Startup briefing context injection | **Complete** |
| WO-15 | SSE real-time UI | **Complete** |
| WO-16 | File tracking + progressive disclosure search | **Complete** |
| WO-17 | Settings panel with live briefing preview | **Complete** |
| WO-18 | UI polish — project filter, infinite scroll, token economics | **Complete** |
| WO-19 | Knowledge agents — queryable memory brains | **Complete** |
| WO-20 | Import progress display and background execution | **Complete** |
| WO-21 | Smart model selection & context window config | **Complete** |
| WO-22 | Re-extraction command | **Complete** |
| WO-23 | Extraction tests — null event_bus handling | **Complete** |
| WO-24 | Update .gitignore with missing patterns | **Complete** |
| WO-25 | Centralize test fixtures in conftest.py | **Complete** |
| WO-26 | Settings route integration tests | **Complete** |
| WO-27 | MCP tool tests — vault review + untested tools | **Complete** |
| WO-28 | False positive marking + Ollama client tests | **Complete** |
| WO-29 | Migration cleanup — document dead scan_status column | **Complete** |
| WO-30 | Timestamp fixes — preserve import times, relative UI formatting | **Complete** |
| WO-31 | Wire event_bus to worker for SSE entity push | **Complete** |
| WO-32 | Session isolation for imports | **Complete** |
| WO-33 | Queue status indicator — live SSE badge in header | **Complete** |
| WO-34 | Feed filtering — type pills, search, ordering | **Complete** |
| WO-04c | Auto-patch AGENTS.md with MCP tool instructions | **Complete** |
| WO-35 | One-command installer | **Complete** |
| WO-36 | Claude Code MCP integration | **Complete** |
| WO-37 | Entity staleness detection | **Complete** |
| WO-38 | Concept tags | Not started |
| WO-39 | Refactor entity_type | Not started |
| WO-40 | Date-range column on FTS | Not started |
| WO-41 | Tool filtering | Not started |
| WO-42 | File-read gate | Not started |
| WO-43 | Endless mode | Not started |
| WO-44 | Mode system | Not started |
| WO-45 | Messaging integrations | Not started |
| WO-46 | Multi-agent tracking | Not started |
| WO-47 | Vector / semantic search | Not started |
| WO-48 | Claude Code session ingestion (live tailer + batch import) | **Complete** |

## Ad-hoc Tasks

- [x] Verify WO-01 acceptance criteria are fully met before moving on
- [ ] Consider adding a `py.typed` marker for PEP 561
- [ ] Set up `ruff` pre-commit hook
- [ ] Pre-existing lint: `SIM108` in `cli.py:1152` (eta ternary) — not from WO-37.
- [ ] Pre-existing lint: `F541` / `F401` in `scripts/setup.py` (gpu_scan imports, f-string).
