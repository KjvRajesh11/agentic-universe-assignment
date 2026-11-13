"""Data normalisation utilities for headers, phones, dates, decimals, enums, and timestamps."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

# Standard IST timezone: UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")


def normalise_header(header: str) -> str:
    """
    Normalise CSV header per specification:
    lower-case, trim, and replace every run of '_' or whitespace with a single space.
    """
    trimmed = header.strip().lower()
    return re.sub(r"[\s_]+", " ", trimmed)


def normalise_phone(raw_phone: str | None) -> tuple[str | None, bool]:
    """
    Normalise Indian phone number:
    - Strip every non-digit
    - If 12 digits starting with '91', drop '91'
    - If 11 digits starting with '0', drop '0'
    - Valid only if exactly 10 digits and starts with 6, 7, 8, or 9
    - Returns (canonical_phone, is_valid), where canonical_phone is '+91XXXXXXXXXX'
    """
    if raw_phone is None:
        return None, False

    stripped = raw_phone.strip()
    if not stripped:
        return None, False

    # Extract all digits
    digits = re.sub(r"\D", "", stripped)

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}", True

    return None, False


def mask_phone(canonical_phone: str | None) -> str:
    """Return masked phone: +91****** followed by last 4 digits."""
    if not canonical_phone or len(canonical_phone) < 4:
        return ""
    return f"+91******{canonical_phone[-4:]}"


_DATE_FORMATS = [
    "%Y-%m-%d",  # 2026-10-01
    "%d/%m/%Y",  # 01/10/2026
    "%d-%m-%Y",  # 01-10-2026
    "%d-%b-%Y",  # 01-Dec-2026
]


def parse_date(raw_date: str | None) -> tuple[str | None, bool]:
    """
    Parse date using day-first logic.
    Accepts: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY.
    Returns (canonical_date_string_YYYY_MM_DD, is_valid).
    """
    if raw_date is None:
        return None, False

    s = raw_date.strip()
    if not s:
        return None, False

    # Month name is case-insensitive for %d-%b-%Y (e.g. 01-dec-2026 -> 01-Dec-2026)
    # Python strptime %b is case-insensitive on most platforms, but title-casing the month helps.
    parts = s.split("-")
    if len(parts) == 3 and len(parts[1]) == 3 and parts[1].isalpha():
        s = f"{parts[0]}-{parts[1].capitalize()}-{parts[2]}"

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d"), True
        except ValueError:
            continue

    return None, False


_CURRENCY_PREFIX_REGEX = re.compile(r"^(₹|rs\.?|inr)\s*", re.IGNORECASE)


def parse_decimal(raw_decimal: str | None) -> tuple[float | None, bool]:
    """
    Parse decimal amount:
    - Optionally prefixed by ₹, Rs, Rs., INR (case-insensitive, optional space)
    - Commas as thousands separators
    - Must be >= 0
    - Round to 2 decimal places
    """
    if raw_decimal is None:
        return None, False

    s = raw_decimal.strip()
    if not s:
        return None, False

    # Remove currency prefix
    cleaned = _CURRENCY_PREFIX_REGEX.sub("", s).strip()
    # Remove commas
    cleaned = cleaned.replace(",", "")

    try:
        val = float(cleaned)
        if val < 0:
            return None, False
        return round(val, 2), True
    except ValueError:
        return None, False


def parse_enum(
    raw_enum: str | None, allowed_values: list[str]
) -> tuple[str | None, bool]:
    """
    Parse enum value:
    - Compare case-insensitively against allowed values
    - Store lowercase
    """
    if raw_enum is None:
        return None, False

    s = raw_enum.strip().lower()
    if not s:
        return None, False

    allowed_lower = {v.lower(): v.lower() for v in allowed_values}
    if s in allowed_lower:
        return allowed_lower[s], True

    return None, False


def parse_string(raw_str: str | None) -> tuple[str | None, bool]:
    """Trim string; empty or whitespace is considered missing."""
    if raw_str is None:
        return None, True
    s = raw_str.strip()
    if not s:
        return None, True
    return s, True


def parse_timestamp_to_ist(raw_ts: str) -> tuple[datetime, str, str, float]:
    """
    Parse timestamp string to an aware IST datetime:
    - UTC timestamps ending in 'Z'
    - Offset-aware ISO timestamps (e.g. +05:30)
    - Naive timestamps (interpreted as Asia/Kolkata IST)

    Returns:
        (aware_ist_dt, iso_ist_str, ist_date_YYYY_MM_DD, epoch_seconds)
    """
    s = raw_ts.strip()
    if not s:
        raise ValueError("Timestamp string cannot be empty")

    if s.endswith("Z"):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        ist_dt = dt.astimezone(IST)
    elif "T" in s:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            ist_dt = dt.replace(tzinfo=IST)
        else:
            ist_dt = dt.astimezone(IST)
    else:
        # Standard naive timestamp like "2026-10-03 10:01:00"
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            ist_dt = dt.replace(tzinfo=IST)
        except ValueError:
            # Fallback for ISO with space instead of T
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                ist_dt = dt.replace(tzinfo=IST)
            else:
                ist_dt = dt.astimezone(IST)

    iso_str = ist_dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    date_str = ist_dt.strftime("%Y-%m-%d")
    epoch_sec = ist_dt.timestamp()

    return ist_dt, iso_str, date_str, epoch_sec
