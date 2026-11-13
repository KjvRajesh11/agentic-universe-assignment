"""Unit tests for report generation contracts: headers, formats, precision, sorting."""

import csv
import json
import tempfile
from pathlib import Path

import pytest

from solution.database import Database
from solution.disposition_service import DispositionService
from solution.event_linking import EventLinkingService
from solution.event_processor import EventProcessor
from solution.lead_ingestion import LeadIngestionService
from solution.models import DataQualityCounters
from solution.pii_protection import PIIProtector
from solution.reporting import ReportGenerator
from solution.schema_loader import SchemaLoader


@pytest.fixture
def populated_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db = Database(tmp_path / "test.db")
        schema_loader = SchemaLoader("assignment-a-data/schemas")
        enc_key = b"0123456789abcdef0123456789abcdef"
        hmac_key = b"hmac_secret_key_at_least_16_bytes_long"
        pii_protector = PIIProtector(enc_key, hmac_key)
        disp_service = DispositionService(schema_loader)
        linking_service = EventLinkingService(pii_protector, disp_service)
        lead_service = LeadIngestionService(db, schema_loader, pii_protector)
        processor = EventProcessor(db, pii_protector, disp_service, linking_service)
        reporter = ReportGenerator(db, schema_loader, disp_service)

        # 1. Ingest leads: 2 valid leads, 1 rejected row
        csv_leads = (
            "customer_name,phone,policy_number,premium_amount\n"
            "Aarav Sharma,+91 98765 43210,MOT/001,12000\n"
            "Diya Patel,+91 91234 56789,MOT/002,15000\n"
            "Invalid Customer,5123456789,MOT/003,5000\n"
        )
        (tmp_path / "batch_001.csv").write_text(csv_leads, encoding="utf-8")
        counters = DataQualityCounters()
        lead_service.ingest_lead_file(
            "drop_1", "motor_renewal", tmp_path / "batch_001.csv", counters
        )

        # 2. Ingest events:
        # Lead 1 (+91 98765 43210): Call 1 connected and renewed (success & terminal)
        # Lead 2 (+91 91234 56789): Call 2 telephony failed
        events = [
            # Call 1 (Lead 1)
            {
                "event_id": "evt_c1_init",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "+91 98765 43210",
            },
            {
                "event_id": "evt_c1_conn",
                "event_type": "call.connected",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:10Z",
                "phone": "+91 98765 43210",
            },
            {
                "event_id": "evt_c1_disp",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "+91 98765 43210",
                "payload": {"disposition_code": "renewed"},
            },
            # Call 2 (Lead 2)
            {
                "event_id": "evt_c2_init",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c2",
                "occurred_at": "2026-10-05T11:00:00Z",
                "phone": "+91 91234 56789",
            },
            {
                "event_id": "evt_c2_fail",
                "event_type": "call.telephony_failed",
                "workflow_id": "motor_renewal",
                "call_id": "c2",
                "occurred_at": "2026-10-05T11:00:05Z",
                "phone": "+91 91234 56789",
                "payload": {"reason": "sip_503"},
            },
        ]
        ev_file = tmp_path / "events.jsonl"
        ev_file.write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )
        processor.process_event_file("drop_1", ev_file, counters)

        yield {
            "reporter": reporter,
            "reports_dir": tmp_path / "reports",
            "counters": counters,
        }


def test_batch_summary_contract(populated_env):
    reporter = populated_env["reporter"]
    out_dir = populated_env["reports_dir"]
    report_file = out_dir / "batch_summary.csv"

    reporter.generate_batch_summary(report_file)
    assert report_file.exists()

    with open(report_file, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    # Check exact header
    expected_header = [
        "workflow_id",
        "batch_id",
        "total_leads",
        "yet_to_reach_out",
        "in_progress_telephony_failed",
        "in_progress_dialed",
        "completed",
    ]
    assert reader[0] == expected_header

    # Row checks: 2 valid leads in batch_001
    # Lead 1: completed (renewed is terminal)
    # Lead 2: in_progress_telephony_failed (only 1 call, and it was telephony failed)
    assert len(reader) == 2
    data_row = reader[1]
    assert data_row[0] == "motor_renewal"
    assert data_row[1] == "batch_001"
    assert data_row[2] == "2"  # total_leads
    assert data_row[3] == "0"  # yet_to_reach_out
    assert data_row[4] == "1"  # in_progress_telephony_failed
    assert data_row[5] == "0"  # in_progress_dialed
    assert data_row[6] == "1"  # completed


def test_daily_overview_contract(populated_env):
    reporter = populated_env["reporter"]
    out_dir = populated_env["reports_dir"]
    report_file = out_dir / "daily_overview.csv"

    reporter.generate_daily_overview(report_file)
    assert report_file.exists()

    with open(report_file, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    expected_header = [
        "workflow_id",
        "date",
        "total_calls",
        "connected_calls",
        "connectivity_rate",
        "success_leads",
    ]
    assert reader[0] == expected_header

    # Total calls = 2 (c1, c2), connected = 1 (c1), connectivity_rate = 1/2 = 0.5000, success_leads = 1 (Lead 1)
    assert len(reader) == 2
    row = reader[1]
    assert row[0] == "motor_renewal"
    assert row[1] == "2026-10-05"
    assert row[2] == "2"
    assert row[3] == "1"
    assert row[4] == "0.5000"
    assert row[5] == "1"


def test_lead_report_contract(populated_env):
    reporter = populated_env["reporter"]
    out_dir = populated_env["reports_dir"]
    report_file = out_dir / "lead_report.csv"

    reporter.generate_lead_report(report_file)
    assert report_file.exists()

    with open(report_file, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    expected_header = [
        "workflow_id",
        "external_ref",
        "batch_id",
        "phone_masked",
        "bucket",
        "total_calls",
        "connected_calls",
        "last_call_at",
        "current_disposition",
        "ever_success",
    ]
    assert reader[0] == expected_header

    # Sorted by workflow_id, external_ref: MOT/001 then MOT/002
    assert len(reader) == 3
    row1 = reader[1]
    assert row1[1] == "MOT/001"
    assert row1[3] == "+91******3210"
    assert row1[4] == "completed"
    assert row1[5] == "1"
    assert row1[6] == "1"
    assert row1[7] == "2026-10-05T15:30:00+05:30"  # 10:00 UTC = 15:30 IST
    assert row1[8] == "renewed"
    assert row1[9] == "true"

    row2 = reader[2]
    assert row2[1] == "MOT/002"
    assert row2[3] == "+91******6789"
    assert row2[4] == "in_progress_telephony_failed"
    assert row2[5] == "1"
    assert row2[6] == "0"
    assert row2[8] == ""  # no disposition
    assert row2[9] == "false"  # not success


def test_rejections_report_contract(populated_env):
    reporter = populated_env["reporter"]
    out_dir = populated_env["reports_dir"]
    report_file = out_dir / "rejections.csv"

    reporter.generate_rejections_report(report_file)
    assert report_file.exists()

    with open(report_file, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    expected_header = ["workflow_id", "batch_id", "row_number", "reason"]
    assert reader[0] == expected_header

    # Row 3 was rejected with invalid_phone
    assert len(reader) == 2
    row = reader[1]
    assert row[0] == "motor_renewal"
    assert row[1] == "batch_001"
    assert row[2] == "3"
    assert row[3] == "invalid_phone"


def test_data_quality_json_contract(populated_env):
    reporter = populated_env["reporter"]
    out_dir = populated_env["reports_dir"]
    report_file = out_dir / "data_quality.json"

    reporter.generate_data_quality(report_file)
    assert report_file.exists()

    with open(report_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    expected_keys = [
        "lead_rows_total",
        "lead_rows_rejected",
        "rejections_by_reason",
        "duplicate_lead_rows_merged",
        "field_warnings",
        "events_total",
        "malformed_events",
        "duplicate_events_dropped",
        "orphan_events",
        "unknown_disposition_events",
    ]
    assert list(data.keys()) == expected_keys

    assert data["lead_rows_total"] == 3
    assert data["lead_rows_rejected"] == 1
    assert data["rejections_by_reason"] == {"invalid_phone": 1}
    assert data["events_total"] == 5
    assert data["orphan_events"] == 0
