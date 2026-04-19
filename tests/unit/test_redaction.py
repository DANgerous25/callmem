"""Tests for sensitive data detection and redaction."""

from __future__ import annotations

from callmem.core.redaction import (
    DEFAULT_ALLOWLIST,
    Detection,
    PatternScanner,
    apply_redactions,
    luhn_check,
    merge_detections,
)


class TestLuhnCheck:
    def test_valid_visa(self) -> None:
        assert luhn_check("4532015112830366") is True

    def test_invalid_visa(self) -> None:
        assert luhn_check("4532015112830367") is False

    def test_valid_mastercard(self) -> None:
        assert luhn_check("5500000000000004") is True

    def test_too_short(self) -> None:
        assert luhn_check("123456") is False

    def test_all_zeros(self) -> None:
        assert luhn_check("0000000000000000") is True


class TestPatternDetection:
    def test_detects_aws_key(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("key = AKIAIOSFODNN7EXAMPLE")
        assert len(hits) == 1
        assert hits[0].category == "secret"
        assert hits[0].pattern_name == "aws_access_key"

    def test_detects_github_token(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("token: ghp_ABCDEFghijklmnopqrstuvwxyz1234567890")
        assert any(h.pattern_name == "github_token" for h in hits)

    def test_detects_openai_key(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012")
        assert any(h.category == "secret" for h in hits)

    def test_detects_jwt(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("token = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456")
        assert any(h.pattern_name == "jwt" for h in hits)

    def test_detects_private_key(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowI...\n-----END RSA PRIVATE KEY-----"
        )
        assert any(h.pattern_name == "private_key" for h in hits)

    def test_detects_email(self) -> None:
        scanner = PatternScanner(detect_categories={"secret", "credential", "pii", "financial"})
        hits = scanner.scan("contact: admin@company.com")
        assert any(h.pattern_name == "email" for h in hits)

    def test_detects_db_connection_string(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("DB=postgres://user:pass@db.example.com:5432/mydb")
        assert any(h.pattern_name == "db_connection" for h in hits)

    def test_detects_bearer_token(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234")
        assert any(h.pattern_name == "bearer_token" for h in hits)


class TestCreditCardDetection:
    def test_valid_luhn_detected(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("card: 4532015112830366")
        cc_hits = [h for h in hits if h.pattern_name == "credit_card"]
        assert len(cc_hits) == 1

    def test_invalid_luhn_not_detected(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("card: 4532015112830367")
        cc_hits = [h for h in hits if h.pattern_name == "credit_card"]
        assert len(cc_hits) == 0


class TestAllowlist:
    def test_test_email_not_flagged(self) -> None:
        scanner = PatternScanner(detect_categories={"secret", "credential", "pii", "financial"})
        hits = scanner.scan("email test@example.com")
        assert not any(h.pattern_name == "email" for h in hits)

    def test_localhost_not_flagged(self) -> None:
        PatternScanner()
        assert "127.0.0.1" in DEFAULT_ALLOWLIST

    def test_non_allowlisted_email_flagged(self) -> None:
        scanner = PatternScanner(detect_categories={"secret", "credential", "pii", "financial"})
        hits = scanner.scan("email admin@real-company.com")
        assert any(h.pattern_name == "email" for h in hits)


class TestEntropyDetection:
    def test_entropy_in_secret_context(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("api_key = aBcDeFgHiJkLmNoPqRsTuVwXyZ123456")
        assert any(h.detector == "entropy" for h in hits)

    def test_entropy_not_flagged_without_context(self) -> None:
        scanner = PatternScanner()
        hits = scanner.scan("The quick aBcDeFgHiJkLmNoPqRsTuVwXyZ123456 fox")
        assert not any(h.detector == "entropy" for h in hits)


class TestApplyRedactions:
    def test_redaction_replaces_value(self) -> None:
        content = "key = sk-proj-abc123def456ghi789jkl012"
        scanner = PatternScanner()
        detections = scanner.scan(content)
        redacted = apply_redactions(content, detections)
        assert "sk-proj" not in redacted
        assert "[REDACTED:" in redacted

    def test_redacted_content_preserves_context(self) -> None:
        content = "Set OPENAI_API_KEY=sk-abc123def456ghi789jkl012 here"
        scanner = PatternScanner()
        detections = scanner.scan(content)
        redacted = apply_redactions(content, detections)
        assert "OPENAI_API_KEY=" in redacted
        assert "[REDACTED:" in redacted

    def test_no_detections_returns_original(self) -> None:
        content = "nothing sensitive here"
        redacted = apply_redactions(content, [])
        assert redacted == content

    def test_multiple_detections(self) -> None:
        content = "AWS key AKIAIOSFODNN7EXAMPLE and token ghp_ABCDEFghijklmnopqrstuvwxyz1234567890"
        scanner = PatternScanner()
        detections = scanner.scan(content)
        redacted = apply_redactions(content, detections)
        assert "AKIA" not in redacted
        assert "ghp_" not in redacted
        assert redacted.count("[REDACTED:") >= 2


class TestMergeDetections:
    def test_non_overlapping_kept(self) -> None:
        a = Detection(
            vault_id="a", category="secret", detector="pattern",
            pattern_name="x", original_value="x", start=0, end=5,
        )
        b = Detection(
            vault_id="b", category="secret", detector="pattern",
            pattern_name="y", original_value="y", start=10, end=15,
        )
        merged = merge_detections([a], [b])
        assert len(merged) == 2

    def test_overlapping_prefers_longer(self) -> None:
        a = Detection(
            vault_id="a", category="secret", detector="pattern",
            pattern_name="x", original_value="longer", start=0, end=10,
        )
        b = Detection(
            vault_id="b", category="secret", detector="llm",
            pattern_name=None, original_value="short", start=2, end=7,
        )
        merged = merge_detections([a], [b])
        assert len(merged) == 1
        assert merged[0].vault_id == "a"

    def test_empty_inputs(self) -> None:
        assert merge_detections([], []) == []
