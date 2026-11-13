"""Cryptographic PII protection module implementing AES-256-GCM and HMAC-SHA256."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from solution.exceptions import PIIConfigurationError, PIIDecryptionError


class PIIProtector:
    """
    Handles authenticated encryption (AES-256-GCM) for sensitive fields
    and deterministic HMAC-SHA256 fingerprinting for lead identity resolution.
    """

    def __init__(self, encryption_key: bytes, hmac_secret: bytes) -> None:
        if not encryption_key or len(encryption_key) != 32:
            raise PIIConfigurationError(
                f"AES-256-GCM requires exactly 32-byte key; received {len(encryption_key) if encryption_key else 0} bytes."
            )
        if not hmac_secret or len(hmac_secret) < 16:
            raise PIIConfigurationError(
                f"HMAC secret requires at least 16 bytes; received {len(hmac_secret) if hmac_secret else 0} bytes."
            )

        self._encryption_key = encryption_key
        self._hmac_secret = hmac_secret
        self._aesgcm = AESGCM(self._encryption_key)

    def encrypt(self, plaintext: str | None) -> str | None:
        """
        Encrypt a string using AES-256-GCM.
        Returns a base64-encoded string: nonce (12 bytes) + ciphertext + tag.
        """
        if plaintext is None:
            return None

        # 96-bit (12-byte) random nonce as recommended for AES-GCM
        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        # Store as base64: nonce + ciphertext (which includes authentication tag)
        combined = nonce + ciphertext
        return base64.b64encode(combined).decode("ascii")

    def decrypt(self, encrypted_b64: str | None) -> str | None:
        """
        Decrypt and authenticate a base64-encoded AES-256-GCM ciphertext.
        Raises PIIDecryptionError if ciphertext is corrupted or tampered.
        """
        if encrypted_b64 is None:
            return None

        try:
            raw_bytes = base64.b64decode(encrypted_b64.encode("ascii"))
            if len(raw_bytes) < 28:  # 12-byte nonce + 16-byte tag minimum
                raise PIIDecryptionError("Invalid ciphertext length.")

            nonce = raw_bytes[:12]
            ciphertext_and_tag = raw_bytes[12:]
            decrypted = self._aesgcm.decrypt(nonce, ciphertext_and_tag, None)
            return decrypted.decode("utf-8")
        except (InvalidTag, ValueError) as e:
            raise PIIDecryptionError(f"Authentication/decryption failed: {e}") from e

    def compute_phone_fingerprint(self, canonical_phone: str) -> str:
        """
        Compute deterministic HMAC-SHA256 fingerprint for canonical phone (+91XXXXXXXXXX).
        Used for indexing and matching leads without plaintext storage.
        """
        if not canonical_phone:
            raise ValueError("Canonical phone cannot be empty for fingerprinting.")

        h = hmac.new(self._hmac_secret, canonical_phone.encode("utf-8"), hashlib.sha256)
        return h.hexdigest()

    def verify_phone_fingerprint(
        self, canonical_phone: str, expected_fingerprint: str
    ) -> bool:
        """Verify phone fingerprint using constant-time comparison."""
        actual = self.compute_phone_fingerprint(canonical_phone)
        return hmac.compare_digest(actual, expected_fingerprint)

    def mask_phone(self, canonical_phone: str | None) -> str:
        """Produce masked phone number +91******XXXX for reports."""
        if not canonical_phone or len(canonical_phone) < 4:
            return ""
        return f"+91******{canonical_phone[-4:]}"
