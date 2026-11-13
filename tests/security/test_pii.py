"""Security and cryptographic unit tests: AES-256-GCM, HMAC-SHA256, fail-closed."""

import base64
import logging

import pytest

from solution.exceptions import PIIConfigurationError, PIIDecryptionError
from solution.logging_config import PIIRedactionFilter
from solution.pii_protection import PIIProtector


@pytest.fixture
def keys():
    enc_key = b"0123456789abcdef0123456789abcdef"  # exactly 32 bytes
    hmac_key = b"hmac_secret_key_at_least_16_bytes_long"
    return enc_key, hmac_key


def test_encryption_roundtrip(keys):
    enc_key, hmac_key = keys
    protector = PIIProtector(enc_key, hmac_key)

    plaintext = "Aarav Sharma"
    encrypted = protector.encrypt(plaintext)
    assert encrypted != plaintext
    assert isinstance(encrypted, str)

    decrypted = protector.decrypt(encrypted)
    assert decrypted == plaintext


def test_distinct_nonces_per_encryption(keys):
    enc_key, hmac_key = keys
    protector = PIIProtector(enc_key, hmac_key)

    plaintext = "Same Name"
    enc1 = protector.encrypt(plaintext)
    enc2 = protector.encrypt(plaintext)
    # Different random IVs must produce distinct ciphertexts
    assert enc1 != enc2
    assert protector.decrypt(enc1) == plaintext
    assert protector.decrypt(enc2) == plaintext


def test_tampered_ciphertext_fails_authentication(keys):
    enc_key, hmac_key = keys
    protector = PIIProtector(enc_key, hmac_key)

    encrypted = protector.encrypt("Confidential Data")
    raw = bytearray(base64.b64decode(encrypted))
    # Flip the last byte of authentication tag
    raw[-1] ^= 0xFF
    tampered = base64.b64encode(raw).decode("ascii")

    with pytest.raises(PIIDecryptionError):
        protector.decrypt(tampered)


def test_fail_closed_on_invalid_keys():
    # Key too short
    with pytest.raises(PIIConfigurationError):
        PIIProtector(b"too_short", b"valid_hmac_secret_16_bytes")

    # HMAC key too short
    with pytest.raises(PIIConfigurationError):
        PIIProtector(b"0123456789abcdef0123456789abcdef", b"short")

    # None keys
    with pytest.raises(PIIConfigurationError):
        PIIProtector(None, b"valid_hmac_secret_16_bytes")


def test_hmac_phone_fingerprinting(keys):
    enc_key, hmac_key = keys
    protector = PIIProtector(enc_key, hmac_key)

    phone = "+919876543210"
    fp1 = protector.compute_phone_fingerprint(phone)
    fp2 = protector.compute_phone_fingerprint(phone)
    # Deterministic matching
    assert fp1 == fp2
    assert len(fp1) == 64  # SHA-256 hex string

    # Different phone produces different fingerprint
    fp_diff = protector.compute_phone_fingerprint("+919876543211")
    assert fp1 != fp_diff

    # Different secret key produces different fingerprint
    protector2 = PIIProtector(enc_key, b"different_secret_key_16_bytes")
    fp_diff_key = protector2.compute_phone_fingerprint(phone)
    assert fp1 != fp_diff_key

    # Constant-time verification
    assert protector.verify_phone_fingerprint(phone, fp1) is True
    assert protector.verify_phone_fingerprint(phone, fp_diff) is False


def test_pii_logging_filter():
    redaction_filter = PIIRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Dialling user at +91 98765 43210 for call call_001",
        args=(),
        exc_info=None,
    )
    redaction_filter.filter(record)
    assert "+91 98765 43210" not in record.msg
    assert "+91******3210" in record.msg
