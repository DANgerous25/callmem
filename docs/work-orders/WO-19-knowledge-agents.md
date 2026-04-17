# WO-19: Knowledge Agents — Queryable Memory Brains

## Priority: P2

## Objective

Implement corpus-based "knowledge agents" — filtered subsets of observation history that can be queried with natural language. This lets OpenCode (or the user) ask questions like "How did we implement authentication?" and get synthesized answers from real project history rather than raw search results.

## Reference

claude-mem's Knowledge Agents have 6 MCP tools: `build_corpus`, `list_corpora`, `prime_corpus`, `query_corpus`, `rebuild_corpus`, `reprime_corpus`. They build JSON corpus files from filtered observations, load them into an LLM session, and answer questions.

---

## Architecture

### Simpler than claude-mem

claude-mem uses the Claude Agent SDK's `resume` option for persistent LLM sessions. We don't have that with Ollama. Instead, we'll use a simpler approach:

1. **Build corpus** — query entities with filters, save as a JSON file
2. **Query corpus** — load the corpus file, concatenate into a context block, and send to Ollama with the user's question as a single prompt

This trades session persistence for simplicity. Each query re-sends the corpus (which is fine for local Ollama — no API cost concern).

### Data Model

Add a `corpora` table:

```sql
CREATE TABLE IF NOT EXISTS corpora (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    project_id TEXT,
    filters TEXT NOT NULL,          -- JSON: {types: [], date_start, date_end, file_paths: [], query}
    entity_count INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Corpus entity membership stored in a junction table:

```sql
CREATE TABLE IF NOT EXISTS corpus_entities (
    corpus_id TEXT NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (corpus_id, entity_id)
);
```

### Core Module

Create `src/llm_mem/core/knowledge.py`:

```python
class KnowledgeAgent:
    def __init__(self, db: Database, ollama: OllamaClient):
        self.db = db
        self.ollama = ollama

    def build_corpus(
        self,
        name: str,
        project_id: str | None = None,
        types: list[str] | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        file_paths: list[str] | None = None,
        query: str | None = None,
    ) -> dict:
        """Build a corpus from filtered entities."""

    def list_corpora(self) -> list[dict]:
        """List all corpora with stats."""

    def query_corpus(self, corpus_name: str, question: str) -> str:
        """Ask a question against a corpus. Returns synthesized answer."""

    def rebuild_corpus(self, corpus_name: str) -> dict:
        """Rebuild corpus with latest entities matching original filters."""
```

### Query Implementation

```python
def query_corpus(self, corpus_name: str, question: str) -> str:
    # 1. Load corpus entities
    entities = self._load_corpus_entities(corpus_name)

    # 2. Format as context block
    context = self._format_corpus_context(entities)

    # 3. Build prompt
    prompt = KNOWLEDGE_QUERY_PROMPT.format(
        context=context,
        question=question,
    )

    # 4. Send to Ollama
    response = self.ollama._generate(prompt)
    return response
```

### Prompt

Add to `src/llm_mem/core/prompts.py`:

```python
KNOWLEDGE_QUERY_PROMPT = """You are a knowledge agent with access to a curated corpus of project observations.
Answer the question using ONLY the information in the corpus below. If the corpus doesn't contain enough information, say so.
Cite observation IDs (e.g., #E01K...) when referencing specific observations.

CORPUS:
{context}

QUESTION: {question}

ANSWER:"""
```

### MCP Tools

Add to `src/llm_mem/mcp/server.py`:

#### `build_corpus`
```
Parameters:
  name: str           — Unique name for this corpus
  types: list[str]    — Entity types to include (optional, default all)
  date_start: str     — Start date YYYY-MM-DD (optional)
  date_end: str       — End date YYYY-MM-DD (optional)
  file_paths: list[str] — Filter by associated files (optional)
  query: str          — FTS5 search filter (optional)

Returns: Corpus stats (entity count, token count)
```

#### `list_corpora`
```
No parameters.
Returns: Table of all corpora with name, entity count, token count, created date.
```

#### `query_corpus`
```
Parameters:
  corpus_name: str    — Name of corpus to query
  question: str       — Natural language question

Returns: Synthesized answer with observation ID citations.
```

#### `rebuild_corpus`
```
Parameters:
  corpus_name: str    — Name of corpus to rebuild

Returns: Updated corpus stats.
```

### CLI Commands

Add to `src/llm_mem/cli.py`:

```bash
llm-mem corpus build <name> [--types bugfix,feature] [--since 2026-04-01] [--query "auth"]
llm-mem corpus list
llm-mem corpus query <name> "How did we implement auth?"
llm-mem corpus rebuild <name>
llm-mem corpus delete <name>
```

### Web UI (optional, lower priority)

A `/knowledge` page listing corpora with a query input box. Can be deferred to a future WO.

---

## Files to Create

- `src/llm_mem/core/knowledge.py` — KnowledgeAgent class

## Files to Modify

- `src/llm_mem/core/database.py` — add `corpora` and `corpus_entities` tables
- `src/llm_mem/core/prompts.py` — add KNOWLEDGE_QUERY_PROMPT
- `src/llm_mem/mcp/server.py` — add 4 knowledge tools
- `src/llm_mem/cli.py` — add `corpus` command group
- `src/llm_mem/core/engine.py` — expose KnowledgeAgent via engine

## Acceptance Criteria

1. [ ] `corpora` and `corpus_entities` tables exist
2. [ ] `build_corpus` creates a corpus from filtered entities
3. [ ] `query_corpus` sends corpus + question to Ollama and returns synthesized answer
4. [ ] Answers cite observation IDs
5. [ ] `list_corpora` returns all corpora with stats
6. [ ] `rebuild_corpus` refreshes a corpus with latest entities matching original filters
7. [ ] All 4 MCP tools work (build, list, query, rebuild)
8. [ ] CLI commands work: `corpus build`, `corpus list`, `corpus query`, `corpus rebuild`, `corpus delete`
9. [ ] Corpus respects token budget (warn if corpus exceeds Ollama context window)
10. [ ] All existing tests pass, new tests for KnowledgeAgent
11. [ ] `make lint` clean, `make test` all pass
12. [ ] Committed and pushed
