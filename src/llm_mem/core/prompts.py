"""Prompt templates for memory-maintenance LLM operations.

These prompts are sent to the local Ollama model, NOT the interactive coding model.
See WO-06 and WO-08 for usage.
"""

from __future__ import annotations

EXTRACTION_PROMPT = """Analyze this coding session exchange and extract structured information.

Events:
{events_text}

Extract the following (only include items that are clearly present — do not invent):

1. **Decisions**: What was decided and why
2. **TODOs**: Tasks mentioned that need to be done (with priority if stated)
3. **Facts**: Durable project knowledge (e.g., "the API uses cursor-based pagination")
4. **Failures**: What went wrong, error messages, whether resolved
5. **Discoveries**: Notable insights or learnings

Respond in this exact JSON format:
{{
  "decisions": [{{"title": "...", "content": "..."}}],
  "todos": [{{"title": "...", "content": "...", "priority": "high|medium|low", "status": "open"}}],
  "facts": [{{"title": "...", "content": "..."}}],
  "failures": [{{"title": "...", "content": "...", "status": "unresolved|resolved"}}],
  "discoveries": [{{"title": "...", "content": "..."}}]
}}

If a category has no items, use an empty array. Do not include explanatory text outside the JSON."""


CHUNK_SUMMARY_PROMPT = """Summarize this batch of coding session events into a concise paragraph.
Focus on: what was worked on, what was accomplished, any problems encountered.

Events:
{events_text}

Write a 2-3 sentence summary. Be specific about files, functions, and technical details."""


SESSION_SUMMARY_PROMPT = """Summarize this entire coding session.

Session chunks:
{chunks_text}

Remaining events not yet summarized:
{remaining_events_text}

Produce a structured summary:
1. **What was done**: Main accomplishments
2. **Key decisions**: Important choices made
3. **Issues**: Problems encountered or unresolved
4. **TODOs**: Tasks identified for future work

Be concise but specific."""


CROSS_SESSION_PROMPT = """Synthesize these session summaries into a project-level overview.

Sessions:
{sessions_text}

Produce a concise project status covering:
1. Current state of the project
2. Major decisions and their rationale
3. Active work streams
4. Known issues and technical debt

Keep it under {max_tokens} tokens."""


BRIEFING_COMPRESSION_PROMPT = """Compress this session briefing to fit within {max_tokens} tokens.
Preserve all active TODOs, unresolved failures, and recent decisions.
Remove or shorten lower-priority context.

Original briefing:
{briefing_text}

Produce a compressed version that retains the most important information."""
