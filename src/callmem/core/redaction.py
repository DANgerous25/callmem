"""Sensitive data detection and redaction.

Two-layer detection:
  Layer 1: Pattern matching (regex + entropy) — fast, deterministic
  Layer 2: Local LLM scan (Ollama) — contextual, catches what patterns miss

Both layers run at ingest time. The local LLM is on the user's own hardware,
so there is no privacy concern about feeding it raw content.

See docs/sensitive-data.md for the full design.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ulid import ULID

if TYPE_CHECKING:
    from collections.abc import Callable

# ── Built-in pattern library ──────────────────────────────────────────

PATTERNS: dict[str, tuple[str, str]] = {
    # (regex, category)

    # Cloud providers
    "aws_access_key":   (r"AKIA[0-9A-Z]{16}", "secret"),
    "aws_secret_key":   (r"(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+=]{40}", "secret"),

    # Code hosting
    "github_token":     (r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}", "secret"),
    "github_pat":       (r"github_pat_[A-Za-z0-9_]{82}", "secret"),
    "gitlab_token":     (r"glpat-[A-Za-z0-9\-]{20,}", "secret"),

    # LLM providers
    "openai_key":       (r"sk-[A-Za-z0-9\-]{20,}", "secret"),
    "anthropic_key":    (r"sk-ant-[A-Za-z0-9\-]{90,}", "secret"),

    # Payment / SaaS
    "stripe_key":       (r"(sk|pk|rk)_(test|live)_[A-Za-z0-9]{24,}", "secret"),
    "sendgrid_key":     (r"SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}", "secret"),

    # Generic secrets
    "private_key": (
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ED25519 )?PRIVATE KEY-----",
        "secret",
    ),
    "jwt": (
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        "secret",
    ),
    "bearer_token": (
        r"(?i)(?:bearer|token)\s+[A-Za-z0-9_\-.~+/]{20,}",
        "credential",
    ),
    "basic_auth":       (r"(?i)basic\s+[A-Za-z0-9+/=]{20,}", "credential"),

    # Connection strings
    "db_connection": (
        r"(?i)(?:postgres|mysql|mongodb|redis|amqp)://[^\s]+:[^\s]+@[^\s]+",
        "credential",
    ),
    "url_with_creds":   (r"https?://[^\s:]+:[^\s@]+@[^\s]+", "credential"),

    # Financial
    "credit_card": (
        r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b",
        "financial",
    ),

    # PII (basic)
    "email":            (r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", "pii"),
    "ipv4_address": (
        r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
        r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b",
        "infra",
    ),
}

# Default allowlist — never redact these
DEFAULT_ALLOWLIST = frozenset({
    "test@example.com",
    "user@example.com",
    "127.0.0.1",
    "0.0.0.0",
    "localhost",
    "192.168.1.1",
    "10.0.0.1",
})

# Context keywords that suggest an adjacent high-entropy string is a secret
SECRET_CONTEXT_KEYWORDS = frozenset({
    "key", "token", "secret", "password", "passwd", "api_key", "apikey",
    "auth", "credential", "private", "access_key", "secret_key",
})


# LLM scanners over-flag identifiers and paths that are never secrets; these
# shapes are dropped from LLM findings before anything is redacted.
MIN_LLM_VALUE_LEN = 6
DEFAULT_DETECT_CATEGORIES = frozenset({"secret", "credential", "pii", "financial"})
_ULID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_CONSTANT_NAME_RE = re.compile(r"[A-Z][A-Z_]*")  # env-var names, not their values


def plausible_llm_finding(
    value: str,
    category: str,
    categories: frozenset[str] | set[str] = DEFAULT_DETECT_CATEGORIES,
) -> bool:
    """False for LLM findings that are identifiers, names or paths, not secrets."""
    return not (
        category not in categories
        or len(value) < MIN_LLM_VALUE_LEN
        or "[REDACTED:" in value
        or _ULID_RE.fullmatch(value)
        or _UUID_RE.fullmatch(value)
        or _CONSTANT_NAME_RE.fullmatch(value)
        or value.startswith(("/", "~/", "./"))
    )


_VAR_REF_RE = re.compile(r"\$\{?\w+\}?|<[^<>]+>|\{\{.*\}\}")
_KNOWN_PREFIX_RE = re.compile(
    r"(sk-|sk_|pk_|rk_|ghp_|gho_|ghs_|github_pat_|glpat-|xox[abpr]-|AKIA|AIza|eyJ|-----BEGIN)"
)
# A credential word, then at most a short gap ("password is ", "auth with
# admin + ") before the value. Whole words only: not NOPASSWD, authentic.
_CREDENTIAL_CONTEXT_RE = re.compile(
    r"(?i)\b(pass(word|wd|phrase)?|pw|pwd|secret|token|api[_ -]?key|apikey|auth"
    r"|authorization|bearer|credentials?|private[_ -]?key|access[_ -]?key)\b"
    r"[^\n]{0,15}$"
)
_CREDENTIAL_WORD_RE = re.compile(
    r"(?i)(pass(word|wd|phrase)?|secret|token|api[_ -]?key|credential)s?"
)
_PLACEHOLDER_RE = re.compile(r"\[REDACTED:[^\]]*\]")
_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+|[\w-]+\.[A-Za-z]{2,5}")
_AT_RE = re.compile(r"[\w.+-]+@[\w.-]+")
_IPV4_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?")
_TOKEN_STOP = " \t\n\r\"'`(),;<>"
CONTEXT_WINDOW = 60  # chars before a value searched for a credential word


def secret_shaped(value: str) -> bool:
    """True for values that look like a key or token on their own."""
    if _KNOWN_PREFIX_RE.match(value):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{24,}", value):
        return True
    if " " in value or len(value) < 10 or _IDENTIFIER_RE.fullmatch(value):
        return False
    has_alpha = any(c.isalpha() for c in value)
    has_digit = any(c.isdigit() for c in value)
    return has_alpha and has_digit and shannon_entropy(value) >= 3.3


def _enclosing_token(text: str, start: int, end: int) -> str:
    """The whitespace/quote-delimited token that holds ``text[start:end]``."""
    left = start
    while left > 0 and text[left - 1] not in _TOKEN_STOP:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _TOKEN_STOP:
        right += 1
    return text[left:right]


def keep_llm_finding(
    text: str,
    start: int,
    end: int,
    category: str,
    categories: frozenset[str] | set[str] = DEFAULT_DETECT_CATEGORIES,
) -> bool:
    """Whether an LLM finding at ``text[start:end]`` should be redacted.

    The local model flags plenty that isn't secret: words inside paths and
    filenames, SSH key names, usernames, container names, list options.
    Key-shaped values are always kept; anything else needs context, such
    as a credential word just before it.
    """
    value = text[start:end]
    if not plausible_llm_finding(value, category, categories):
        return False
    if _VAR_REF_RE.fullmatch(value) or _CREDENTIAL_WORD_RE.fullmatch(value):
        return False  # "$TOKEN", or the word "password" itself
    if category != "pii" and _AT_RE.fullmatch(value):
        return False  # user@host; real emails are pii and the regex catches them
    if _IPV4_RE.fullmatch(value):
        return False  # addresses are infra, not secrets
    if secret_shaped(value) and shannon_entropy(value) >= 3.5:
        return True
    token = _enclosing_token(text, start, end)
    if "/" in token or "\\" in token:
        return False
    if text[end:end + 1] == "@" and text[end + 1:end + 2].isalnum():
        return False  # user@host
    digits = sum(c.isdigit() for c in value)
    if category == "pii":
        return (
            digits >= 7
            or bool(re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.]+", value))
            or bool(re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z'-]+)+", value))
        )
    if category == "financial":
        return digits >= 6
    if secret_shaped(value):
        return True
    if (
        _CREDENTIAL_WORD_RE.search(value)
        and any(c.isdigit() for c in value)
        and not _IDENTIFIER_RE.fullmatch(value)
    ):
        return True  # "secret123", "Password1": a weak password, not a name
    line_start = text.rfind("\n", 0, start) + 1
    before = text[max(line_start, start - CONTEXT_WINDOW):start]
    # Neighbouring placeholders say "credential" too; that isn't context.
    return bool(_CREDENTIAL_CONTEXT_RE.search(_PLACEHOLDER_RE.sub("", before)))


# ── Data types ────────────────────────────────────────────────────────

@dataclass
class Detection:
    """A single detected sensitive value."""
    vault_id: str
    category: str       # secret, credential, pii, financial, infra
    detector: str       # pattern, entropy, llm
    pattern_name: str | None
    original_value: str
    start: int
    end: int
    confidence: float = 1.0


class IncompleteScanError(Exception):
    """The scanner answered, but not with a whole findings list.

    Usually the reply hit the token cap because the text held many
    findings; scanning smaller pieces gets complete answers.
    """


# Splitting stops here: below this a cut-off reply isn't a size problem.
MIN_SPLIT_CHARS = 500
SPLIT_OVERLAP = 100  # so a value on the cut line is whole in one half


def scan_in_pieces(
    scan: Callable[[str], list[Detection] | None],
    text: str,
    min_chars: int = MIN_SPLIT_CHARS,
) -> list[Detection] | None:
    """Run ``scan``, halving the text whenever the reply comes back cut off.

    Returns None if the scanner didn't answer, or if a piece of
    ``min_chars`` or less still gets a cut-off reply. Offsets in the
    result are relative to ``text``.
    """
    try:
        return scan(text)
    except IncompleteScanError:
        if len(text) <= min_chars:
            return None
    mid = len(text) // 2
    space = text.rfind(" ", mid - SPLIT_OVERLAP, mid)
    mid = space if space != -1 else mid
    right_start = max(0, mid - SPLIT_OVERLAP)
    left = scan_in_pieces(scan, text[:mid + SPLIT_OVERLAP], min_chars)
    if left is None:
        return None
    right = scan_in_pieces(scan, text[right_start:], min_chars)
    if right is None:
        return None
    shifted = [
        replace(d, start=d.start + right_start, end=d.end + right_start) for d in right
    ]
    return merge_detections(left, shifted)


@dataclass
class RedactionResult:
    """Result of scanning and redacting content."""
    redacted_content: str
    detections: list[Detection] = field(default_factory=list)
    scan_status: str = "full"  # full, pattern_only


# ── Entropy ───────────────────────────────────────────────────────────

def shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string. Higher = more random."""
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


# ── Pattern scanner ───────────────────────────────────────────────────

class PatternScanner:
    """Layer 1: Regex + entropy based detection."""

    def __init__(
        self,
        patterns: dict[str, tuple[str, str]] | None = None,
        allowlist: frozenset[str] | None = None,
        entropy_threshold: float = 4.5,
        entropy_min_length: int = 20,
        detect_categories: set[str] | None = None,
    ) -> None:
        self.allowlist = allowlist or DEFAULT_ALLOWLIST
        self.entropy_threshold = entropy_threshold
        self.entropy_min_length = entropy_min_length
        self.detect_categories = detect_categories or set(DEFAULT_DETECT_CATEGORIES)

        # Compile patterns, filtered by enabled categories
        src = patterns or PATTERNS
        self._compiled: list[tuple[str, re.Pattern[str], str]] = []
        for name, (regex, category) in src.items():
            if category in self.detect_categories:
                self._compiled.append((name, re.compile(regex), category))

    def scan(self, content: str) -> list[Detection]:
        """Scan content for pattern matches and high-entropy strings."""
        detections: list[Detection] = []
        seen_ranges: set[tuple[int, int]] = set()

        # Regex patterns
        for name, pattern, category in self._compiled:
            for match in pattern.finditer(content):
                value = match.group(0)
                if value in self.allowlist:
                    continue
                span = (match.start(), match.end())
                if span in seen_ranges:
                    continue
                seen_ranges.add(span)
                # Validate credit cards with Luhn
                if name == "credit_card" and not luhn_check(value):
                    continue
                detections.append(Detection(
                    vault_id=str(ULID()),
                    category=category,
                    detector="pattern",
                    pattern_name=name,
                    original_value=value,
                    start=match.start(),
                    end=match.end(),
                ))

        # Entropy detection on word-like tokens
        if "secret" in self.detect_categories:
            for token_match in re.finditer(r"[A-Za-z0-9_\-+/=.]{20,}", content):
                token = token_match.group(0)
                span = (token_match.start(), token_match.end())
                if span in seen_ranges:
                    continue
                if token in self.allowlist:
                    continue
                if len(token) < self.entropy_min_length:
                    continue
                if shannon_entropy(token) < self.entropy_threshold:
                    continue
                # Check surrounding context for secret-like keywords
                context_start = max(0, token_match.start() - 50)
                context = content[context_start:token_match.start()].lower()
                if any(kw in context for kw in SECRET_CONTEXT_KEYWORDS):
                    seen_ranges.add(span)
                    detections.append(Detection(
                        vault_id=str(ULID()),
                        category="secret",
                        detector="entropy",
                        pattern_name=None,
                        original_value=token,
                        start=token_match.start(),
                        end=token_match.end(),
                        confidence=0.8,
                    ))

        return detections


# ── Redactor ──────────────────────────────────────────────────────────

def apply_redactions(content: str, detections: list[Detection]) -> str:
    """Replace detected values in content with redaction tokens.

    Processes detections in reverse order to preserve character offsets.
    """
    # Sort by start position descending
    sorted_detections = sorted(detections, key=lambda d: d.start, reverse=True)
    result = content
    for d in sorted_detections:
        token = f"[REDACTED:{d.category}:{d.vault_id}]"
        result = result[:d.start] + token + result[d.end:]
    return result


def merge_detections(
    pattern_hits: list[Detection], llm_hits: list[Detection]
) -> list[Detection]:
    """Merge detections from both layers, preferring longer matches for overlaps."""
    all_detections = pattern_hits + llm_hits
    all_detections.sort(key=lambda d: (d.start, -(d.end - d.start)))

    merged: list[Detection] = []
    last_end = -1
    for d in all_detections:
        if d.start >= last_end:
            merged.append(d)
            last_end = d.end
    return merged


def luhn_check(number: str) -> bool:
    """Validate a number string using the Luhn algorithm."""
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0
