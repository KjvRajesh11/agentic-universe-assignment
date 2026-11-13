"""Structured logging configuration with PII redaction filters."""

from __future__ import annotations

import logging
import re
import sys


class PIIRedactionFilter(logging.Filter):
    """Filter that masks potential phone numbers and sensitive data patterns in log records."""

    # Matches Indian mobile numbers with optional +91 and optional whitespace/hyphens between digits
    _PHONE_PATTERN = re.compile(r"(?:\+?91[\s-]?)?(?:[6-9](?:[\s-]?\d){9})")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._mask_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._mask_val(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._mask_val(v) for v in record.args)
        return True

    def _mask_val(self, val: object) -> object:
        if isinstance(val, str):
            return self._mask_text(val)
        return val

    def _mask_text(self, text: str) -> str:
        def _replace_phone(m: re.Match[str]) -> str:
            raw_match = m.group(0)
            digits = re.sub(r"\D", "", raw_match)
            if len(digits) >= 10:
                last4 = digits[-4:]
                return f"+91******{last4}"
            return raw_match

        return self._PHONE_PATTERN.sub(_replace_phone, text)


def configure_logging(level: str = "INFO") -> None:
    """Configure root and application loggers."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(log_level)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    handler.addFilter(PIIRedactionFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
