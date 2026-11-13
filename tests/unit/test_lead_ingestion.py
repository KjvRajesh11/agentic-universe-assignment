"""Unit tests for lead ingestion: validation, rejections, warnings, merging, aliases."""

import tempfile
from pathlib import Path

import pytest

from solution.database import Database
from solution.lead_ingestion import LeadIngestionService
from solution.models import DataQualityCounters
from solution.pii_protection import PIIProtector
from solution.schema_loader import SchemaLoader


@pytest.fixture
def test_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)
        schema_loader = SchemaLoader("assignment-a-data/schemas")
        enc_key = b"0123456789abcdef0123456789abcdef"
        hmac_key = b"hmac_secret_key_at_least_16_bytes_long"
        pii_protector = PIIProtector(enc_key, hmac_key)
        service = LeadIngestionService(db, schema_loader, pii_protector)
        yield {
            "db": db,
            "schema_loader": schema_loader,
            "pii_protector": pii_protector,
            "service": service,
            "tmpdir": Path(tmpdir),
        }


def test_lead_ingestion_valid_row(test_env):
    service = test_env["service"]
    db = test_env["db"]
    tmpdir = test_env["tmpdir"]

    csv_content = (
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "Aarav Sharma,+91 98765 43210,MOT/2026/100001,2026-10-15,12000,hi\n"
    )
    csv_file = tmpdir / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    counters = DataQualityCounters()
    service.ingest_lead_file("drop_1", "motor_renewal", csv_file, counters)

    assert counters.lead_rows_total == 1
    assert counters.lead_rows_rejected == 0
    assert counters.duplicate_lead_rows_merged == 0
    assert counters.field_warnings == 0

    with db.get_connection() as conn:
        lead = conn.execute(
            "SELECT * FROM leads WHERE workflow_id = 'motor_renewal'"
        ).fetchone()
        assert lead is not None
        assert lead["batch_id"] == "batch_001"
        assert lead["phone_masked"] == "+91******3210"
        assert lead["external_ref"] == "MOT/2026/100001"
        # Plaintext phone is NOT stored
        assert "+919876543210" not in lead["encrypted_phone"]
        # Decrypt phone
        decrypted_phone = test_env["pii_protector"].decrypt(lead["encrypted_phone"])
        assert decrypted_phone == "+919876543210"


def test_blank_rows_skipped(test_env):
    service = test_env["service"]
    tmpdir = test_env["tmpdir"]

    csv_content = (
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "\n"
        "   ,   ,   ,   ,   ,   \n"
        "Aarav Sharma,+91 98765 43210,MOT/2026/100001,2026-10-15,12000,hi\n"
        "\n"
    )
    csv_file = tmpdir / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    counters = DataQualityCounters()
    service.ingest_lead_file("drop_1", "motor_renewal", csv_file, counters)

    # Only 1 non-blank data row
    assert counters.lead_rows_total == 1
    assert counters.lead_rows_rejected == 0


def test_schema_order_rejections(test_env):
    service = test_env["service"]
    db = test_env["db"]
    tmpdir = test_env["tmpdir"]

    # motor_renewal fields: customer_name (1), phone (2), policy_number (3)
    # Row 1: missing customer_name -> missing_required:customer_name (even if phone is also missing!)
    # Row 2: invalid present phone -> invalid_phone
    # Row 3: missing policy_number -> missing_required:policy_number
    csv_content = (
        "customer_name,phone,policy_number\n"
        ",5123456789,MOT/2026/001\n"
        "Rahul Verma,5123456789,MOT/2026/002\n"
        "Sneha Rao,+919876543210,\n"
    )
    csv_file = tmpdir / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    counters = DataQualityCounters()
    service.ingest_lead_file("drop_1", "motor_renewal", csv_file, counters)

    assert counters.lead_rows_total == 3
    assert counters.lead_rows_rejected == 3
    assert counters.rejections_by_reason == {
        "missing_required:customer_name": 1,
        "invalid_phone": 1,
        "missing_required:policy_number": 1,
    }

    with db.get_connection() as conn:
        rejections = conn.execute(
            "SELECT row_number, reason FROM rejected_leads ORDER BY row_number"
        ).fetchall()
        assert len(rejections) == 3
        assert rejections[0]["row_number"] == 1
        assert rejections[0]["reason"] == "missing_required:customer_name"
        assert rejections[1]["row_number"] == 2
        assert rejections[1]["reason"] == "invalid_phone"
        assert rejections[2]["row_number"] == 3
        assert rejections[2]["reason"] == "missing_required:policy_number"


def test_field_warnings_on_valid_rows(test_env):
    service = test_env["service"]
    tmpdir = test_env["tmpdir"]

    # Valid lead but invalid optional fields: expiry date "31/02/2026" and premium "N/A"
    csv_content = (
        "customer_name,phone,policy_number,policy_expiry,premium_amount\n"
        "Aarav Sharma,+91 98765 43210,MOT/2026/100001,31/02/2026,N/A\n"
    )
    csv_file = tmpdir / "batch_001.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    counters = DataQualityCounters()
    service.ingest_lead_file("drop_1", "motor_renewal", csv_file, counters)

    assert counters.lead_rows_total == 1
    assert counters.lead_rows_rejected == 0
    # Two optional invalid fields -> 2 warnings
    assert counters.field_warnings == 2


def test_duplicate_lead_merging(test_env):
    service = test_env["service"]
    db = test_env["db"]
    tmpdir = test_env["tmpdir"]

    # First row in batch_001
    csv1 = (
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "Aarav Sharma,+91 98765 43210,MOT/2026/100001,2026-10-15,12000,hi\n"
    )
    f1 = tmpdir / "batch_001.csv"
    f1.write_text(csv1, encoding="utf-8")

    counters = DataQualityCounters()
    service.ingest_lead_file("drop_1", "motor_renewal", f1, counters)
    assert counters.lead_rows_total == 1
    assert counters.duplicate_lead_rows_merged == 0

    # Second row in batch_002 with same phone:
    # premium updated to 15000, preferred_language is empty/null (should NOT erase 'hi')
    csv2 = (
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "Aarav Sharma,+91 98765 43210,MOT/2026/100001,2026-10-15,15000,\n"
    )
    f2 = tmpdir / "batch_002.csv"
    f2.write_text(csv2, encoding="utf-8")

    service.ingest_lead_file("drop_1", "motor_renewal", f2, counters)
    assert counters.lead_rows_total == 2
    assert counters.duplicate_lead_rows_merged == 1

    with db.get_connection() as conn:
        lead = conn.execute(
            "SELECT * FROM leads WHERE workflow_id = 'motor_renewal'"
        ).fetchone()
        assert lead["batch_id"] == "batch_001"  # permanent original batch_id!
        import json

        fields = json.loads(lead["field_values_json"])
        assert fields["premium_amount"] == 15000.0  # overwritten
        assert fields["preferred_language"] == "hi"  # NOT erased by null


def test_header_aliases_and_reordered_columns(test_env):
    service = test_env["service"]
    tmpdir = test_env["tmpdir"]

    # Using alias headers in different order and extra unmapped column "agent_notes"
    csv_content = (
        "Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes\n"
        "ta,Priya Iyer,MOT/2026/999999,91-88888-77777,2026-11-20,Rs. 8,500,VIP customer\n"
    )
    csv_file = tmpdir / "batch_003.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    counters = DataQualityCounters()
    service.ingest_lead_file("drop_2", "motor_renewal", csv_file, counters)

    assert counters.lead_rows_total == 1
    assert counters.lead_rows_rejected == 0
    assert counters.field_warnings == 0
