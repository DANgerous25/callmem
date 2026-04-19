"""Encrypted vault for sensitive data.

Stores original values of redacted content using Fernet encryption
(AES-128-CBC + HMAC). Two key modes:
  - auto: random key stored in .callmem/vault.key (default)
  - passphrase: key derived from CALLMEM_VAULT_PASSPHRASE env var via scrypt

See docs/sensitive-data.md for the full design.
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


class VaultKeyManager:
    """Manages the Fernet encryption key for the vault."""

    def __init__(self, callmem_dir: Path, mode: str = "auto") -> None:
        self.callmem_dir = callmem_dir
        self.mode = mode
        self._fernet: Fernet | None = None

    def get_fernet(self) -> Fernet:
        """Get or initialize the Fernet instance."""
        if self._fernet is not None:
            return self._fernet

        if self.mode == "passphrase":
            self._fernet = self._from_passphrase()
        else:
            self._fernet = self._from_auto_key()

        return self._fernet

    def _from_auto_key(self) -> Fernet:
        """Generate or load a random key stored on disk."""
        key_path = self.callmem_dir / "vault.key"
        if key_path.exists():
            key = key_path.read_bytes().strip()
        else:
            self.callmem_dir.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            key_path.write_bytes(key)
            key_path.chmod(0o600)
        return Fernet(key)

    def _from_passphrase(self) -> Fernet:
        """Derive key from passphrase using scrypt."""
        passphrase = os.environ.get("CALLMEM_VAULT_PASSPHRASE") or os.environ.get(
            "LLM_MEM_VAULT_PASSPHRASE"
        )
        if not passphrase:
            raise ValueError(
                "Vault mode is 'passphrase' but CALLMEM_VAULT_PASSPHRASE "
                "environment variable is not set."
            )

        salt_path = self.callmem_dir / "vault.salt"
        if salt_path.exists():
            salt = salt_path.read_bytes()
        else:
            salt = os.urandom(16)
            salt_path.write_bytes(salt)
            salt_path.chmod(0o600)

        kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))
        return Fernet(key)

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt a string and return ciphertext bytes."""
        return self.get_fernet().encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt ciphertext bytes and return the original string."""
        try:
            return self.get_fernet().decrypt(ciphertext).decode("utf-8")
        except InvalidToken as e:
            raise ValueError("Failed to decrypt vault entry — wrong key or corrupted data.") from e
