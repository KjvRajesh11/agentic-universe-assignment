"""Application configuration and environment variable loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from solution.exceptions import PIIConfigurationError


@dataclass(frozen=True)
class AppConfig:
    """Holds pipeline runtime configuration."""

    db_path: Path
    schema_dir: Path
    dispositions_path: Path
    encryption_key: bytes
    hmac_secret: bytes
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls,
        db_path: Path | str | None = None,
        schema_dir: Path | str | None = None,
        dispositions_path: Path | str | None = None,
        encryption_key: bytes | str | None = None,
        hmac_secret: bytes | str | None = None,
        log_level: str | None = None,
    ) -> AppConfig:
        """Load configuration from environment variables or explicit parameters."""
        resolved_db_path = Path(db_path or os.getenv("DB_PATH", "agentic.db")).resolve()

        resolved_schema_dir = Path(
            schema_dir or os.getenv("SCHEMA_DIR", "assignment-a-data/schemas")
        ).resolve()

        resolved_dispositions_path = Path(
            dispositions_path
            or os.getenv(
                "DISPOSITIONS_FILE", str(resolved_schema_dir / "dispositions.csv")
            )
        ).resolve()

        # Encryption key
        raw_enc_key = (
            encryption_key
            or os.getenv("APP_ENCRYPTION_KEY")
            or os.getenv("PII_ENCRYPTION_KEY")
        )
        enc_bytes = cls._parse_key(
            raw_enc_key, "APP_ENCRYPTION_KEY", expected_length=32
        )

        # HMAC secret
        raw_hmac_secret = (
            hmac_secret or os.getenv("APP_HMAC_SECRET") or os.getenv("PII_HMAC_SECRET")
        )
        hmac_bytes = cls._parse_key(raw_hmac_secret, "APP_HMAC_SECRET", min_length=16)

        resolved_log_level = log_level or os.getenv("LOG_LEVEL", "INFO").upper()

        return cls(
            db_path=resolved_db_path,
            schema_dir=resolved_schema_dir,
            dispositions_path=resolved_dispositions_path,
            encryption_key=enc_bytes,
            hmac_secret=hmac_bytes,
            log_level=resolved_log_level,
        )

    @staticmethod
    def _parse_key(
        val: bytes | str | None,
        name: str,
        expected_length: int | None = None,
        min_length: int | None = None,
    ) -> bytes:
        if val is None:
            raise PIIConfigurationError(
                f"Missing required security configuration: '{name}' must be set in environment."
            )

        if isinstance(val, bytes):
            key_bytes = val
        elif isinstance(val, str):
            val_stripped = val.strip()
            # Try hex
            if len(val_stripped) == 64 and all(
                c in "0123456789abcdefABCDEF" for c in val_stripped
            ):
                key_bytes = bytes.fromhex(val_stripped)
            else:
                key_bytes = val_stripped.encode("utf-8")
        else:
            raise PIIConfigurationError(f"Invalid key type for '{name}'.")

        if expected_length and len(key_bytes) != expected_length:
            raise PIIConfigurationError(
                f"Invalid key length for '{name}': expected {expected_length} bytes, got {len(key_bytes)}."
            )

        if min_length and len(key_bytes) < min_length:
            raise PIIConfigurationError(
                f"Invalid key length for '{name}': requires at least {min_length} bytes, got {len(key_bytes)}."
            )

        return key_bytes
