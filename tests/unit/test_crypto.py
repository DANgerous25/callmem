"""Tests for encrypted vault (crypto module)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from llm_mem.core.crypto import VaultKeyManager


class TestAutoMode:
    def test_encrypt_decrypt_roundtrip(self, tmp_path: Path) -> None:
        km = VaultKeyManager(tmp_path, mode="auto")
        original = "sk-proj-abc123def456ghi789"
        ct = km.encrypt(original)
        assert km.decrypt(ct) == original

    def test_key_file_created(self, tmp_path: Path) -> None:
        km = VaultKeyManager(tmp_path, mode="auto")
        km.get_fernet()
        assert (tmp_path / "vault.key").exists()

    def test_key_file_permissions(self, tmp_path: Path) -> None:
        km = VaultKeyManager(tmp_path, mode="auto")
        km.get_fernet()
        key_path = tmp_path / "vault.key"
        mode = key_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_different_dirs_different_keys(self, tmp_path: Path) -> None:
        km1 = VaultKeyManager(tmp_path / "a", mode="auto")
        km2 = VaultKeyManager(tmp_path / "b", mode="auto")
        ct = km1.encrypt("secret")
        with pytest.raises(ValueError, match="Failed to decrypt"):
            km2.decrypt(ct)

    def test_encrypt_decrypt_multiple_values(self, tmp_path: Path) -> None:
        km = VaultKeyManager(tmp_path, mode="auto")
        secrets = ["password123", "AKIAIOSFODNN7EXAMPLE", "sk-abc123"]
        for s in secrets:
            ct = km.encrypt(s)
            assert km.decrypt(ct) == s

    def test_same_key_on_reload(self, tmp_path: Path) -> None:
        km1 = VaultKeyManager(tmp_path, mode="auto")
        ct = km1.encrypt("test")

        km2 = VaultKeyManager(tmp_path, mode="auto")
        assert km2.decrypt(ct) == "test"


class TestPassphraseMode:
    def test_encrypt_decrypt_with_passphrase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MEM_VAULT_PASSPHRASE", "test-passphrase")
        km = VaultKeyManager(tmp_path, mode="passphrase")
        ct = km.encrypt("my secret")
        assert km.decrypt(ct) == "my secret"

    def test_passphrase_creates_salt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MEM_VAULT_PASSPHRASE", "test-passphrase")
        km = VaultKeyManager(tmp_path, mode="passphrase")
        km.get_fernet()
        assert (tmp_path / "vault.salt").exists()

    def test_same_passphrase_same_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MEM_VAULT_PASSPHRASE", "stable-passphrase")
        km1 = VaultKeyManager(tmp_path, mode="passphrase")
        ct = km1.encrypt("test")

        km2 = VaultKeyManager(tmp_path, mode="passphrase")
        assert km2.decrypt(ct) == "test"

    def test_missing_passphrase_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LLM_MEM_VAULT_PASSPHRASE", raising=False)
        km = VaultKeyManager(tmp_path, mode="passphrase")
        with pytest.raises(ValueError, match="passphrase"):
            km.get_fernet()
