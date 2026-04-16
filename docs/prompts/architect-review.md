# Prompt: Architect Review

Use this prompt when asking an AI architect to review a work order before implementation, or to review completed implementation for architectural soundness.

## Pre-implementation review

```
You are reviewing a technical work order for the llm-mem project — a persistent memory system for coding agents.

Project context:
- Python backend, SQLite + FTS5 storage, local Ollama for background LLM work
- MCP server for agent integration (OpenCode first)
- See README.md and docs/architecture.md for full context

Work order to review:
{paste work order content}

Review for:
1. **Feasibility**: Can this be implemented as described? Any missing prerequisites?
2. **Interface quality**: Are the proposed interfaces clean, testable, and consistent with the rest of the system?
3. **Edge cases**: What edge cases are not addressed? What could break?
4. **Dependency issues**: Any circular dependencies, missing imports, or version conflicts?
5. **Test coverage**: Are the suggested tests sufficient? What's missing?
6. **Simplification**: Can anything be simplified without losing functionality?

Output:
- ✅ Approved items (no changes needed)
- ⚠️ Concerns (should be addressed but not blocking)
- 🚫 Blockers (must be fixed before implementation)
- Specific suggestions for each concern/blocker
```

## Post-implementation review

```
You are reviewing a completed implementation for the llm-mem project.

Work order that was implemented:
{paste work order content}

Files changed:
{list files or paste diff}

Review for:
1. **Contract compliance**: Does the implementation match the work order's acceptance criteria?
2. **Code quality**: Is the code clean, well-structured, and idiomatic Python?
3. **Error handling**: Are errors handled appropriately? Any unhandled edge cases?
4. **Test quality**: Do the tests actually verify the acceptance criteria? Are there gaps?
5. **Performance**: Any obvious performance issues (N+1 queries, missing indexes, etc.)?
6. **Security**: Any SQL injection, path traversal, or other security concerns?
7. **Integration**: Will this work correctly with the rest of the system?

Output a structured review with specific file:line references for each finding.
```

## Architecture decision review

```
A design decision needs to be made for the llm-mem project.

Context:
{describe the decision and why it matters}

Options:
{list options with tradeoffs}

Evaluate each option against these criteria:
1. Simplicity (fewer moving parts wins)
2. Testability
3. Performance at scale (tens of thousands of memories)
4. Compatibility with the existing SQLite + FTS5 + Ollama architecture
5. Ease of implementation by a coding agent (GLM/OpenCode)

Recommend one option and explain why. Be opinionated.
```
