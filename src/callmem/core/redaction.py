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
from dataclasses import dataclass, field

from ulid import ULID

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
        self.detect_categories = detect_categories or {"secret", "credential", "pii", "financial"}

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
