"""Prompt templates for memory-maintenance LLM operations.

These prompts are sent to the local Ollama model,
NOT the interactive coding model.
"""

from __future__ import annotations

# ruff: noqa: E501
EXTRACTION_PROMPT = """Analyze this coding session exchange and extract ONLY significant, non-trivial information.

Events:
{events_text}

PRIOR ENTITIES IN THIS SESSION (do not re-emit near-duplicates of these — if the
events extend or resolve an item below, update it as a different category like
`bugfixes` or `changes` rather than re-creating it):
{prior_titles}

CORE RULES:
- **Dedup hard.** One concept = one entity. The same observation captured as a
  `failure`, then a `discovery`, then a `change`, then another `change` is the
  failure mode we are explicitly trying to avoid. If a prior title above already
  covers the concept, SKIP it. If the events resolve a prior `todo`/`failure`,
  emit one `bugfix`/`change`/`feature` that says how — do not re-emit the
  original.
- **Be concrete.** key_points and content must contain SPECIFIC anchors when the
  events contain them: file paths, line numbers, function/class names, exact
  command strings, exact error messages. A title like "Fix FK constraint in
  vault insertion" beats "Fix database issue" every time. If no concrete anchors
  exist in the events, the entity is probably too vague to keep — skip it.
- **Synopsis is at most one sentence.** Not a narrative paragraph. If you cannot
  say something new beyond what title + key_points already convey, omit the
  synopsis field entirely.
- **Skip the routine.** "ran tests", "pushed changes", "read file X" — these are
  noise. Capture decisions, surprises, failures, and durable facts only.
- **Prefer fewer high-quality items.** A session that produces 2 sharp entities
  is healthier than one producing 9 fuzzy ones.

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
- "title": concise specific summary with concrete anchors when available
  ("Refactor _build_footer_parts in briefing.py:340 to split body/footer"
  beats "Refactor footer rendering logic")
- "content": 1-2 sentences with concrete details (file paths, function names,
  values). No corporate prose ("The application architecture utilizes…").
- "key_points": 2-5 bullets with SPECIFIC technical details. Each bullet should
  name a thing (file, function, value, command) — not restate the title.
- "synopsis": OPTIONAL. At most one sentence, only if it adds something the
  title + key_points don't already convey. Omit if redundant.
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
