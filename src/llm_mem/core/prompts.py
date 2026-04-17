"""Prompt templates for memory-maintenance LLM operations.

These prompts are sent to the local Ollama model, NOT the interactive coding model.
See WO-06 and WO-08 for usage.
"""

from __future__ import annotations

EXTRACTION_PROMPT = """Analyze this coding session exchange and extract structured information.

Events:
{events_text}

Extract the following (only include items that are clearly present — do not invent):

1. **Decisions**: What was decided and why (architectural/design choices)
2. **TODOs**: Tasks mentioned that need to be done (with priority if stated)
3. **Facts**: Durable project knowledge (e.g., "the API uses cursor-based pagination")
4. **Failures**: What went wrong, error messages, whether resolved
5. **Discoveries**: Notable insights or learnings
6. **Features**: New functionality added or implemented
7. **Bugfixes**: Bugs identified and/or fixed
8. **Research**: Investigation, analysis, or exploration work
9. **Changes**: General file or code changes that don't fit other categories

For each extracted item, provide:
- "title": a concise 1-line summary
- "content": a detailed description (1-3 sentences)
- "key_points": a list of 2-5 bullet points capturing the essential information
- "synopsis": a flowing prose paragraph (2-4 sentences) giving full context

Respond in this exact JSON format:
{{
  "decisions": [{{
    "title": "...", "content": "...",
    "key_points": ["point 1", "point 2"],
    "synopsis": "Narrative paragraph..."
  }}],
  "todos": [{{
    "title": "...", "content": "...",
    "priority": "high|medium|low", "status": "open",
    "key_points": ["..."], "synopsis": "..."
  }}],
  "facts": [{{
    "title": "...", "content": "...",
    "key_points": ["..."], "synopsis": "..."
  }}],
  "failures": [{{
    "title": "...", "content": "...",
    "status": "unresolved|resolved",
    "key_points": ["..."], "synopsis": "..."
  }}],
  "discoveries": [{{
    "title": "...", "content": "...",
    "key_points": ["..."], "synopsis": "..."
  }}],
  "features": [{{
    "title": "...", "content": "...",
    "key_points": ["..."], "synopsis": "..."
  }}],
  "bugfixes": [{{
    "title": "...", "content": "...",
    "key_points": ["..."], "synopsis": "..."
  }}],
  "research": [{{
    "title": "...", "content": "...",
    "key_points": ["..."], "synopsis": "..."
  }}],
  "changes": [{{
    "title": "...", "content": "...",
    "key_points": ["..."], "synopsis": "..."
  }}]
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

Produce a structured summary with a clear title line followed by
relevant sections. Use emoji section headers for visual scanning.
Omit sections that have no content (don't write empty sections).

Format:
**[Clear 1-line title describing what was accomplished, NOT the user's raw prompt]**

🔍 INVESTIGATED
- What was explored, researched, or analyzed

💡 LEARNED
- Key insights or discoveries

✅ COMPLETED
- What was actually done or shipped

📋 NEXT STEPS
- Tasks or items identified for future work

Be specific about files, functions, and technical details.
The title should be a human-readable description of the session's outcome."""


CROSS_SESSION_PROMPT = """Synthesize these session summaries into a project-level overview.

Sessions:
{sessions_text}

Produce a concise project status covering:
1. Current state of the project
2. Major decisions and their rationale
3. Active work streams
4. Known issues and technical debt

Keep it under {max_tokens} tokens."""


SENSITIVE_SCAN_PROMPT = (
    """Scan the following text for sensitive data \
that should not be stored in plain text.

Look for:
- Passwords or passphrases (e.g., "the password is hunter2", "passwd: abc123")
- API keys or tokens not in standard format (e.g., inline secrets, custom key formats)
- Database credentials embedded in prose (e.g., "connect with user admin and password xyz")
- Private keys or certificates mentioned in passing
- Personal identifiable information (SSN, phone, address)
- Any other secrets that regex patterns might miss

For each finding, respond with a JSON object containing:
- "value": the exact sensitive string to redact
- "category": one of "secret", "credential", "pii", "financial", "infra"
- "confidence": your confidence level from 0.0 to 1.0

Respond with a JSON array of findings. If nothing sensitive is found, respond with [].

Text to scan:
{text}"""
)


BRIEFING_COMPRESSION_PROMPT = """Compress this session briefing to fit within {max_tokens} tokens.
Preserve all active TODOs, unresolved failures, and recent decisions.
Remove or shorten lower-priority context.

Original briefing:
{briefing_text}

Produce a compressed version that retains the most important information."""
