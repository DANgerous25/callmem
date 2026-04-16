# WO-04b: Sensitive Data Detection and Encrypted Vault

## Objective

Implement the two-layer sensitive data detection (pattern + local LLM), the redaction system, and the encrypted vault. Wire it into the ingest pipeline so that events are scanned and redacted before being stored.

This WO sits between WO-04 (core engine) and WO-05 (MCP server). The ingest pipeline from WO-04 must be working before this WO can be implemented.

## Files to create

- `src/llm_mem/core/redaction.py` — Pattern scanner, entropy detection, redaction logic (skeleton exists — flesh out the full implementation)
- `src/llm_mem/core/crypto.py` — Vault key manager, encrypt/decrypt (skeleton exists — flesh out)
- `src/llm_mem/core/migrations/002_vault.sql` — Vault table and scan_status column
- `tests/unit/test_redaction.py`
- `tests/unit/test_crypto.py`
- `tests/unit/test_sensitive_integration.py`

## Files to modify

- `src/llm_mem/core/engine.py` — Wire redaction into the ingest pipeline
- `src/llm_mem/core/ollama.py` — Add `scan_sensitive()` method using the LLM detection prompt
- `src/llm_mem/core/prompts.py` — Add `SENSITIVE_SCAN_PROMPT`
- `src/llm_mem/core/repository.py` — Add vault CRUD methods
- `pyproject.toml` — Add `cryptography` dependency

## Schema addition

```sql
-- 002_vault.sql

CREATE TABLE IF NOT EXISTS vault (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    category       TEXT NOT NULL,
    detector       TEXT NOT NULL,
    pattern_name   TEXT,
    ciphertext     BLOB NOT NULL,
    created_at     TEXT NOT NULL,
    event_id       TEXT REFERENCES events(id),
    reviewed       INTEGER DEFAULT 0,
    false_positive INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vault_project ON vault(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vault_event ON vault(event_id);

-- Add scan_status to events (nullable for backward compat)
ALTER TABLE events ADD COLUMN scan_status TEXT DEFAULT NULL;
```

## Constraints

- Pattern scan is always synchronous at ingest time
- LLM scan is synchronous at ingest when Ollama is available; events are flagged `scan_status = "pattern_only"` when Ollama is down
- A background job rescans `pattern_only` events when Ollama becomes available
- The `cryptography` library is the only new dependency
- Vault key file permissions must be 0o600 (owner only)
- `apply_redactions()` must handle overlapping detections gracefully (prefer the longer match)
- Credit card detection must validate with Luhn checksum, not just pattern match
- LLM scan results below the confidence threshold are discarded
- Redacted content must remain coherent to both humans and LLMs reading it

## Implementation details

### Ingest flow (modified from WO-04)

```python
def ingest(self, events: list[EventInput]) -> list[Event]:
    stored = []
    for event_input in events:
        content = event_input.content

        # Layer 1: pattern + entropy
        detections = self.pattern_scanner.scan(content)

        # Layer 2: local LLM (if available)
        if self.ollama and self.ollama.is_available():
            llm_detections = self.ollama.scan_sensitive(content)
            # Filter by confidence threshold
            llm_detections = [d for d in llm_detections if d.confidence >= self.config.redaction.llm_scan_confidence]
            # Merge, dedup overlapping ranges
            detections = merge_detections(detections, llm_detections)
            scan_status = "full"
        else:
            scan_status = "pattern_only"

        # Redact
        if detections:
            content = apply_redactions(content, detections)

        # Store redacted event
        event = Event(
            session_id=session.id,
            project_id=self.project_id,
            type=event_input.type,
            content=content,
            metadata={**(event_input.metadata or {}), "scan_status": scan_status},
        )
        self.repository.insert_event(event)

        # Store encrypted originals in vault
        for d in detections:
            self.repository.insert_vault_entry(
                id=d.vault_id,
                project_id=self.project_id,
                category=d.category,
                detector=d.detector,
                pattern_name=d.pattern_name,
                ciphertext=self.crypto.encrypt(d.original_value),
                event_id=event.id,
            )

        stored.append(event)
    return stored
```

### Merge overlapping detections

```python
def merge_detections(pattern_hits: list[Detection], llm_hits: list[Detection]) -> list[Detection]:
    """Merge detections from both layers, preferring pattern matches for overlapping ranges."""
    all_detections = pattern_hits + llm_hits
    all_detections.sort(key=lambda d: (d.start, -(d.end - d.start)))

    merged = []
    last_end = -1
    for d in all_detections:
        if d.start >= last_end:
            merged.append(d)
            last_end = d.end
        # If overlapping, skip the shorter/later detection
    return merged
```

### Luhn validation for credit cards

```python
def luhn_check(number: str) -> bool:
    """Validate a credit card number using the Luhn algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
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
```

## Acceptance criteria

1. Pattern scanner detects AWS keys, GitHub tokens, OpenAI keys, JWTs, private key blocks, emails, credit cards, connection strings
2. Credit card detection validates with Luhn — random 16-digit numbers that fail Luhn are not flagged
3. Entropy detection flags high-entropy strings only when preceded by secret-like context words
4. Allowlisted values (test@example.com, 127.0.0.1, etc.) are never redacted
5. LLM scanner detects "the password is hunter2" style secrets
6. LLM scanner respects confidence threshold — low-confidence findings are dropped
7. When Ollama is unavailable, events are stored with `scan_status = "pattern_only"` and pattern redaction still works
8. Redacted content has `[REDACTED:category:vault_id]` tokens in place of sensitive values
9. Vault entries are encrypted with Fernet and decryptable with the correct key
10. Vault key file has 0o600 permissions
11. Overlapping detections from both layers are merged correctly
12. False positive marking un-redacts the event content
13. `pytest tests/unit/test_redaction.py tests/unit/test_crypto.py tests/unit/test_sensitive_integration.py` passes

## Suggested tests

```python
# Pattern detection
def test_detects_aws_key():
    scanner = PatternScanner()
    hits = scanner.scan("key = AKIAIOSFODNN7EXAMPLE")
    assert len(hits) == 1
    assert hits[0].category == "secret"
    assert hits[0].pattern_name == "aws_access_key"

def test_detects_github_token():
    scanner = PatternScanner()
    hits = scanner.scan("token: ghp_ABCDEFghijklmnopqrstuvwxyz1234567890")
    assert any(h.pattern_name == "github_token" for h in hits)

def test_detects_openai_key():
    scanner = PatternScanner()
    hits = scanner.scan("OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012")
    assert any(h.category == "secret" for h in hits)

def test_credit_card_with_luhn():
    scanner = PatternScanner()
    hits = scanner.scan("card: 4532015112830366")  # Valid Luhn
    assert len(hits) == 1
    hits = scanner.scan("card: 4532015112830367")  # Invalid Luhn
    assert len(hits) == 0

def test_allowlist_respected():
    scanner = PatternScanner()
    hits = scanner.scan("email test@example.com")
    assert len(hits) == 0

def test_entropy_in_secret_context():
    scanner = PatternScanner()
    hits = scanner.scan("api_key = aBcDeFgHiJkLmNoPqRsTuVwXyZ123456")
    assert any(h.detector == "entropy" for h in hits)

def test_entropy_not_flagged_without_context():
    scanner = PatternScanner()
    hits = scanner.scan("The quick aBcDeFgHiJkLmNoPqRsTuVwXyZ123456 fox")
    assert not any(h.detector == "entropy" for h in hits)

# Redaction
def test_redaction_replaces_value():
    content = "key = sk-proj-abc123def456ghi789jkl012"
    scanner = PatternScanner()
    detections = scanner.scan(content)
    redacted = apply_redactions(content, detections)
    assert "sk-proj" not in redacted
    assert "[REDACTED:" in redacted

def test_redacted_content_is_coherent():
    content = "Set OPENAI_API_KEY=sk-abc123 and STRIPE_KEY=sk_test_xyz789"
    scanner = PatternScanner()
    detections = scanner.scan(content)
    redacted = apply_redactions(content, detections)
    assert "OPENAI_API_KEY=" in redacted  # Context preserved
    assert "[REDACTED:" in redacted

# Crypto
def test_encrypt_decrypt_roundtrip(tmp_path):
    km = VaultKeyManager(tmp_path, mode="auto")
    original = "sk-proj-abc123def456ghi789"
    ct = km.encrypt(original)
    assert km.decrypt(ct) == original

def test_wrong_key_fails(tmp_path):
    km1 = VaultKeyManager(tmp_path / "a", mode="auto")
    km2 = VaultKeyManager(tmp_path / "b", mode="auto")
    ct = km1.encrypt("secret")
    with pytest.raises(ValueError):
        km2.decrypt(ct)

def test_passphrase_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MEM_VAULT_PASSPHRASE", "test-passphrase")
    km = VaultKeyManager(tmp_path, mode="passphrase")
    ct = km.encrypt("my secret")
    assert km.decrypt(ct) == "my secret"

def test_key_file_permissions(tmp_path):
    km = VaultKeyManager(tmp_path, mode="auto")
    km.get_fernet()  # Triggers key generation
    key_path = tmp_path / "vault.key"
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"
```
