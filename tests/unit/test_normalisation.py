"""Unit tests for normalisation utilities: phone, date, decimal, enum, headers, timestamps."""

from solution.normalisation import (
    IST,
    mask_phone,
    normalise_header,
    normalise_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_string,
    parse_timestamp_to_ist,
)


def test_header_normalisation():
    assert normalise_header("Mobile No") == "mobile no"
    assert normalise_header("mobile_no") == "mobile no"
    assert normalise_header("  mobile___no  ") == "mobile no"
    assert normalise_header("Customer   Name") == "customer name"
    assert normalise_header("POLICY_NUMBER") == "policy number"
    assert normalise_header("  Order__ID  ") == "order id"


def test_phone_normalisation_valid():
    valid_cases = [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("91-98765-43210", "+919876543210"),
        ("(+91) 9876543210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        ("  +91-88888-99999  ", "+918888899999"),
        ("7123456789", "+917123456789"),
        ("6123456789", "+916123456789"),
    ]
    for raw, expected in valid_cases:
        res, is_valid = normalise_phone(raw)
        assert is_valid is True, f"Failed for {raw}"
        assert res == expected


def test_phone_normalisation_invalid():
    invalid_cases = [
        "987654321",  # 9 digits
        "98765432101",  # 11 digits without leading 0
        "5123456789",  # starts with 5
        "1234567890",  # starts with 1
        "022-2345-6789",  # landline
        "N/A",
        "+1 415 555 0134",  # non-Indian
        "",
        "   ",
        None,
    ]
    for raw in invalid_cases:
        res, is_valid = normalise_phone(raw)
        assert is_valid is False, f"Expected invalid for {raw}"
        assert res is None


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91******3210"
    assert mask_phone("9876543210") == "+91******3210"
    assert mask_phone("") == ""
    assert mask_phone(None) == ""


def test_parse_date_valid():
    cases = [
        ("2026-10-01", "2026-10-01"),
        ("01/10/2026", "2026-10-01"),
        ("01-10-2026", "2026-10-01"),
        ("01-Dec-2026", "2026-12-01"),
        ("01-dec-2026", "2026-12-01"),
        ("01-DEC-2026", "2026-12-01"),
        ("  2026-05-15  ", "2026-05-15"),
    ]
    for raw, expected in cases:
        res, is_valid = parse_date(raw)
        assert is_valid is True, f"Failed for {raw}"
        assert res == expected


def test_parse_date_invalid():
    invalid_cases = [
        "31/02/2026",  # February 31st
        "soon",
        "2026-13-01",  # 13th month
        "TBD",
        "32-10-2026",
        "",
        "   ",
        None,
    ]
    for raw in invalid_cases:
        res, is_valid = parse_date(raw)
        assert is_valid is False, f"Expected invalid for {raw}"
        assert res is None


def test_parse_decimal_valid():
    cases = [
        ("12000", 12000.0),
        ("12000.50", 12000.5),
        ("₹12,000", 12000.0),
        ("Rs. 15,500.75", 15500.75),
        ("Rs 500", 500.0),
        ("INR 9,999.999", 10000.0),  # round to 2 places
        ("0", 0.0),
        ("  ₹ 450.25  ", 450.25),
    ]
    for raw, expected in cases:
        res, is_valid = parse_decimal(raw)
        assert is_valid is True, f"Failed for {raw}"
        assert res == expected


def test_parse_decimal_invalid():
    invalid_cases = [
        "-500",  # negative
        "₹-100",
        "N/A",
        "-",
        "twelve thousand",
        "",
        "   ",
        None,
    ]
    for raw in invalid_cases:
        res, is_valid = parse_decimal(raw)
        assert is_valid is False, f"Expected invalid for {raw}"
        assert res is None


def test_parse_enum():
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    res, is_valid = parse_enum("en", allowed)
    assert is_valid is True and res == "en"

    res, is_valid = parse_enum("HI", allowed)
    assert is_valid is True and res == "hi"

    res, is_valid = parse_enum("  Ta  ", allowed)
    assert is_valid is True and res == "ta"

    res, is_valid = parse_enum("fr", allowed)
    assert is_valid is False and res is None

    res, is_valid = parse_enum("", allowed)
    assert is_valid is False and res is None


def test_parse_string():
    res, is_valid = parse_string("  hello world  ")
    assert is_valid is True and res == "hello world"

    res, is_valid = parse_string("")
    assert is_valid is True and res is None

    res, is_valid = parse_string("   ")
    assert is_valid is True and res is None


def test_parse_timestamp_to_ist():
    # 1. UTC timestamp ending in Z
    utc_str = "2026-10-05T13:20:21Z"
    dt, iso_str, date_str, epoch = parse_timestamp_to_ist(utc_str)
    # 13:20:21 UTC + 05:30 = 18:50:21 IST
    assert date_str == "2026-10-05"
    assert iso_str == "2026-10-05T18:50:21+05:30"
    assert dt.tzinfo == IST

    # 2. Offset-aware ISO timestamp
    offset_str = "2026-10-05T18:50:21+05:30"
    _dt2, iso_str2, date_str2, epoch2 = parse_timestamp_to_ist(offset_str)
    assert date_str2 == "2026-10-05"
    assert iso_str2 == "2026-10-05T18:50:21+05:30"
    assert epoch == epoch2

    # 3. Naive timestamp (interpreted as IST)
    naive_str = "2026-10-05 18:50:21"
    _dt3, iso_str3, date_str3, epoch3 = parse_timestamp_to_ist(naive_str)
    assert date_str3 == "2026-10-05"
    assert iso_str3 == "2026-10-05T18:50:21+05:30"
    assert epoch == epoch3

    # Date crossing midnight UTC -> IST
    utc_night = "2026-10-05T20:00:00Z"
    # 20:00 UTC + 5:30 = 01:30 next day (2026-10-06)
    _, _, date_cross, _ = parse_timestamp_to_ist(utc_night)
    assert date_cross == "2026-10-06"
