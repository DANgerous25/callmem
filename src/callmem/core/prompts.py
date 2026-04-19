"""Prompt templates for memory-maintenance LLM operations.

These prompts are sent to the local Ollama model, NOT the interactive coding model.
See WO-06 and WO-08 for usage.
"""

from __future__ import annotations

EXTRACTION_PROMPT = """Analyze this coding session exchange and extract ONLY significant, non-trivial information.

Events:
{events_text}

RULES:
- Extract ONLY items that provide genuine, durable value to a future developer
- Do NOT extract items that merely restate what happened — focus on WHY and WHAT NEXT
- Do NOT create multiple items for the same concept (e.g., if a bug is found and fixed, create ONE bugfix entry, not a separate failure + todo + bugfix)
- If an event is routine or low-signal (e.g., "ran tests", "pushed changes"), skip it entirely
- key_points must contain SPECIFIC technical details (file paths, function names, error messages, exact config values), not vague restatements
- Prefer fewer high-quality items over many shallow ones

Categories (only include items that are clearly and meaningfully present):

1. **decisions**: Architectural or design choices with rationale — WHY was X chosen over Y
2. **todos**: Concrete tasks with enough detail to act on — must include what file/area and what to do
3. **facts**: Durable project knowledge a new developer would need — e.g., "auth uses JWT with RS256", "the scheduler runs in a goroutine pool of 4"
4. **failures**: Bugs or errors with actual error messages, root cause, and resolution status
5. **features**: Significant new functionality — not trivial UI tweaks or copy changes
6. **bugfixes**: Bugs that were fixed — include root cause and the fix approach
7. **discoveries**: Non-obvious insights — e.g., "SQLite WAL mode doesn't work over NFS"
8. **research**: Investigation that yielded actionable conclusions
9. **changes**: Notable code/architecture changes not covered above

For each item:
- "title": concise specific summary (not generic — "Fix FK constraint in vault insertion" not "Fix database issue")
- "content": 1-3 sentences with concrete details (file paths, function names, values)
- "key_points": 2-5 bullets with SPECIFIC technical details, NOT vague restatements of the title
- "synopsis": 2-4 sentence narrative paragraph giving full context for someone who wasn't there
- "files": file paths mentioned or affected

Respond in this exact JSON format:
{{
  "decisions": [{{
    "title": "...", "content": "...",
    "key_points": ["point 1", "point 2"],
    "synopsis": "Narrative paragraph...",
    "files": ["path/to/file.py"]
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


KNOWLEDGE_QUERY_PROMPT = (
    "You are a knowledge agent with access to a curated corpus of "
    "project observations. Answer the question using ONLY the "
    "information in the corpus below. If the corpus doesn't contain "
    "enough information, say so. Cite observation IDs "
    "(e.g., #E01K...) when referencing specific observations.\n\n"
    "CORPUS:\n{context}\n\nQUESTION: {question}\n\nANSWER:"
)
