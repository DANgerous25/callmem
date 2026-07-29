"""Code anchor parsing and validation.

An "anchor" is a file reference — optionally with a line number — found
in extracted entity content, e.g. "src/callmem/core/briefing.py:340".
Anchors are captured deterministically at extraction time
(``parse_file_anchors``) rather than relying on the LLM to enumerate
files, then revalidated against the live working tree at render/query
time (``validate_anchor``) so a citation degrades gracefully — an
annotation, not a crash — when the code it points at moves or is
deleted.

Security: ``validate_anchor`` must never call ``exists()``/stat a path
that resolves outside the project root. This is a two-stage check:
``is_within_root`` first does a purely lexical containment check
(``os.path.normpath``/``os.path.commonpath`` — no filesystem access at
all) to reject obvious traversal cheaply. But a path that is lexically
inside the root can still be a symlink pointing outside it (e.g.
``.venv/bin/python3``, a ``node_modules`` symlink, or a hostile
symlink), so ``validate_anchor`` then resolves the candidate
(``os.path.realpath``, which *does* follow symlinks) and re-runs the
containment check against the resolved root before ever calling
``exists()``. Only a path that is contained both lexically and after
symlink resolution is stat'd.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Common source/config/doc file extensions. Deliberately a fixed list
# (rather than "any dotted word") to avoid false positives like "e.g."
# or "etc." being parsed as file references.
_FILE_EXTENSIONS = (
    r"py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|kts|c|cc|cpp|cxx|h|hpp|"
    r"rb|php|sh|bash|zsh|sql|json|ya?ml|toml|md|rst|txt|html|htm|css|"
    r"scss|less|vue|svelte|swift|cs|ex|exs|clj|cljs|lua|pl|proto|graphql|"
    r"ini|cfg|conf|env"
)

# Matches "path/to/file.ext" or "file.ext" optionally followed by
# ":<line>". Requires either a path separator or a recognized extension
# so plain prose isn't swept up.
_FILE_REF_RE = re.compile(
    r"(?<![\w/.\-])"
    r"((?:[\w.\-]+/)+[\w.\-]+\.(?:" + _FILE_EXTENSIONS + r")"
    r"|[\w\-]+\.(?:" + _FILE_EXTENSIONS + r"))"
    r"(?::(\d+))?"
    r"(?![\w:])"
)


def parse_file_anchors(text: str) -> list[tuple[str, int | None]]:
    """Extract ``(file_path, line_number)`` references from free text.

    Deterministic regex-based parsing (not LLM-derived), used at
    extraction time to seed ``entity_files`` with anchors precise enough
    to validate later. Deduplicates by file_path, keeping the first line
    number seen for each path.
    """
    if not text:
        return []
    seen: dict[str, int | None] = {}
    for match in _FILE_REF_RE.finditer(text):
        path, line = match.group(1), match.group(2)
        if path not in seen:
            seen[path] = int(line) if line else None
    return list(seen.items())


def is_within_root(file_path: str, project_root: str) -> bool:
    """True if ``file_path`` resolves inside ``project_root``.

    Filesystem-free: uses ``os.path.normpath``/``os.path.commonpath``
    only, never ``Path.resolve()`` (which would follow symlinks via the
    filesystem) or any ``stat`` call. Must be checked before any
    existence check is performed on the path.
    """
    if not project_root or not file_path:
        return False
    root_abs = os.path.normpath(project_root)
    candidate = (
        file_path if os.path.isabs(file_path)
        else os.path.join(project_root, file_path)
    )
    candidate_abs = os.path.normpath(candidate)
    try:
        common = os.path.commonpath([root_abs, candidate_abs])
    except ValueError:
        # Different drives (Windows) or otherwise incomparable paths —
        # treat as outside the root.
        return False
    return common == root_abs


def validate_anchor(file_path: str, project_root: str | None) -> bool | None:
    """Return whether ``file_path`` still exists under ``project_root``.

    Returns ``None`` (unvalidated, not "invalid") when: no project root
    is known, the path is empty, the path resolves outside the project
    root — lexically (``is_within_root``) or, after following symlinks,
    physically (see module docstring) — or the stat call itself raises
    ``OSError`` (permissions, race, etc.). A stat error must never
    surface as an exception or as a false "missing".
    """
    if not project_root or not file_path:
        return None
    if not is_within_root(file_path, project_root):
        return None
    candidate = (
        file_path if os.path.isabs(file_path)
        else os.path.join(project_root, file_path)
    )

    # Lexical containment isn't enough: `candidate` can be (or pass
    # through) a symlink that lives inside the root but points outside
    # it. Resolve it and re-check containment against the resolved
    # root BEFORE calling exists() — a path that escapes the root only
    # after symlink resolution must never be stat'd.
    try:
        resolved_candidate = os.path.realpath(candidate)
        resolved_root = os.path.realpath(project_root)
    except OSError:
        return None
    if not is_within_root(resolved_candidate, resolved_root):
        return None

    try:
        return Path(resolved_candidate).exists()
    except OSError:
        return None
