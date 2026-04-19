# Prompt: Implementer Mode

Use this prompt when giving a work order to a coding agent (GLM/OpenCode) for implementation.

## Standard implementation prompt

```
You are implementing a work order for the callmem project.

## Project overview
callmem is a persistent memory system for coding agents. Python backend, SQLite + FTS5 for storage, local Ollama for background LLM work, MCP server for agent integration.

## Key files to understand first
- README.md — Project overview
- docs/architecture.md — System design
- docs/schema.md — Database schema
- pyproject.toml — Dependencies and project config
- src/callmem/core/database.py — Database module
- src/callmem/models/ — Data models

## Work order
{paste work order content}

## Implementation rules
1. Follow the file list in the work order exactly — create/modify only those files
2. Use the existing data models from src/callmem/models/
3. Use the existing Database class for all SQL operations
4. All SQL must use parameterized queries (never string formatting)
5. All new functions must have type hints
6. Write docstrings for public methods
7. Follow existing code style and patterns
8. Run tests after implementation: `pytest {test_files}`
9. Fix any failing tests before marking complete

## Process
1. Read the files listed in the work order's "Files to create" and "Files to modify" sections
2. Read any files referenced as dependencies
3. Implement the work order
4. Write the tests
5. Run the tests
6. Fix failures
7. Report what you did and any concerns
```

## Incremental implementation prompt

For work orders that build on previous work:

```
You are continuing implementation of the callmem project. Previous work orders have been completed.

## What exists already
{list completed WOs or key files}

## Current work order
{paste work order content}

## Before you start
1. Read the existing implementation of the modules you'll depend on
2. Verify the interfaces match what the work order expects
3. If anything doesn't match, adapt your implementation to the actual interfaces (and note the deviation)

## After implementation
1. Run ALL tests, not just the new ones: `pytest tests/`
2. Ensure no regressions
3. Report any interface mismatches with the work order
```

## Focused implementation prompt

For when you want the agent to implement just one specific function or class:

```
Implement the following in the callmem project:

File: {file_path}
Class/Function: {name}
Purpose: {one-sentence description}

Interface:
```python
{paste interface from work order}
```

Dependencies (already implemented):
- {module}: {what it provides}
- {module}: {what it provides}

Constraints:
- {specific constraint}
- {specific constraint}

Write the implementation and a test for it. Run the test.
```
