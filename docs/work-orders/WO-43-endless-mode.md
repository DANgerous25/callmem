# WO-43 — Endless Mode

## Goal

Enable O(N) scaling for marathon coding sessions by progressively compressing older conversation context and replacing it with llm-mem summaries, so the agent never hits context window limits.

## Background

Long coding sessions hit the context window ceiling. When that happens, the agent either loses early context (sliding window) or the session must end and restart. claude-mem handles this with an "endless mode" that compresses older context in-place.

llm-mem already has chunk summaries and cross-session summaries. This WO extends that to work within a single session: as the conversation grows, older exchanges are compressed into llm-mem observations and replaced in the active context with compact summaries.

## Architecture

### The compression cycle

1. **Monitor context usage**: Track approximate token count of the active conversation
2. **Trigger threshold**: When context reaches 80% of the model's context window (configurable), trigger compression
3. **Select oldest chunk**: Take the oldest N messages that aren't pinned
4. **Compress**: Send the chunk through the existing summarization pipeline, store as entities
5. **Replace in context**: The compressed chunk is replaced with a compact summary pointer: `[Context compressed — 47 messages summarized. Use mem_search to recall details.]`
6. **Continue**: The agent continues with freed context space

### Integration approach

This is the hardest part. The MCP server cannot directly modify the agent's context window — that's controlled by the agent shell (OpenCode/Claude Code). Two viable approaches:

**Approach A: Advisory (recommended for v1)**

The MCP server provides a tool `mem_check_context` that the agent calls periodically (or the AGENTS.md instructions tell it to call every ~20 messages):

```python
@server.tool("mem_check_context")
async def check_context(
    message_count: int,        # approximate messages in current session
    estimated_tokens: int = 0  # if the agent can estimate
) -> dict:
    """Check if context compression is recommended."""
```

Response:
```json
{
  "status": "compress_recommended",
  "reason": "Session has 147 messages (~45k tokens). Compressing oldest 80 messages would free ~25k tokens.",
  "action": "Call mem_compress_context to compress the oldest messages."
}
```

Then `mem_compress_context` stores the compressed summary and tells the agent what to forget.

**Approach B: Agent shell hooks (future)**

If OpenCode or Claude Code exposes context management APIs, hook into them directly. This is the ideal but depends on upstream features. Research needed.

## Deliverables

### 1. Context monitoring tool

```python
@server.tool("mem_check_context")
async def check_context(message_count: int, estimated_tokens: int = 0) -> dict:
```

### 2. Context compression tool

```python
@server.tool("mem_compress_context")
async def compress_context(
    summary: str,              # agent provides a summary of what it's compressing
    message_range: str = "",   # e.g. "messages 1-80"
) -> dict:
```

This:
- Stores the summary as a "chunk_summary" entity
- Links it to the current session
- Returns confirmation with the compressed summary for the agent to use as a replacement marker

### 3. AGENTS.md instructions

```markdown
**Long sessions (50+ messages):**
- Every ~30 messages, call `mem_check_context` with your approximate message count
- If compression is recommended, summarize the oldest messages and call `mem_compress_context`
- Replace the compressed portion of your context with the returned summary marker
- Use `mem_search` to recall specific details from compressed context when needed
```

### 4. Config

```toml
[endless_mode]
enabled = true
context_limit = 128000          # model's context window in tokens
compress_threshold = 0.8        # trigger at 80% usage
chunk_size = 30                 # messages to compress at a time
```

The setup wizard should detect `num_ctx` from the Ollama model config and set `context_limit` automatically.

### 5. Session stats

Track per session:
- Total messages (raw, before compression)
- Compression events (how many times compression triggered)
- Estimated tokens saved
- Show in web UI session detail view

## Constraints

- Python 3.10 compatible
- No AI attribution
- v1 is advisory only (Approach A) — the agent must cooperate via AGENTS.md instructions
- Compression must not lose critical context — decisions, TODOs, and failures should be preserved verbatim in the compressed summary, not further summarized
- Must work with both OpenCode and Claude Code

## Acceptance criteria

- [ ] `mem_check_context` returns compression recommendation when threshold exceeded
- [ ] `mem_compress_context` stores summary and returns confirmation
- [ ] AGENTS.md includes endless mode instructions
- [ ] Config options for threshold and chunk size
- [ ] Session stats track compression events
- [ ] Works with both OpenCode and Claude Code
- [ ] All existing tests pass
