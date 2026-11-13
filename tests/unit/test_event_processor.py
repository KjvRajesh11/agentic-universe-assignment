"""Unit tests for event processing: malformed, duplicates, orphans, unknown dispositions, overrides."""

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
        disp_service = DispositionService(schema_loader)
        linking_service = EventLinkingService(pii_protector, disp_service)
        lead_service = LeadIngestionService(db, schema_loader, pii_protector)
        processor = EventProcessor(db, pii_protector, disp_service, linking_service)

        yield {
            "db": db,
            "schema_loader": schema_loader,
            "pii_protector": pii_protector,
            "disp_service": disp_service,
            "linking_service": linking_service,
            "lead_service": lead_service,
            "processor": processor,
            "tmpdir": Path(tmpdir),
        }


def test_validation_order(test_env):
    processor = test_env["processor"]
    lead_service = test_env["lead_service"]
    db = test_env["db"]
    tmpdir = test_env["tmpdir"]

    # First ingest a lead
    csv_content = (
        "customer_name,phone,policy_number\nAarav Sharma,+91 98765 43210,MOT/2026/001\n"
    )
    (tmpdir / "batch_001.csv").write_text(csv_content, encoding="utf-8")
    lead_service.ingest_lead_file(
        "drop_1", "motor_renewal", tmpdir / "batch_001.csv", DataQualityCounters()
    )

    # Create event JSON lines testing:
    # 1. Malformed JSON line
    # 2. Missing event_id
    # 3. Valid event (call.initiated)
    # 4. Duplicate event (same event_id as #3)
    # 5. Orphan event (phone never uploaded)
    # 6. Unknown disposition code ("INVALID_DISPO")
    # 7. Valid disposition.set
    lines = [
        '{"broken": json',  # 1. malformed
        '{"event_type": "call.initiated", "call_id": "c1"}',  # 2. missing event_id
        json.dumps(
            {  # 3. valid
                "event_id": "evt_001",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "+91 98765 43210",
            }
        ),
        json.dumps(
            {  # 4. duplicate
                "event_id": "evt_001",
                "event_type": "call.connected",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:10Z",
                "phone": "+91 98765 43210",
            }
        ),
        json.dumps(
            {  # 5. orphan
                "event_id": "evt_002",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c2",
                "occurred_at": "2026-10-05T11:00:00Z",
                "phone": "+91 99999 88888",
            }
        ),
        json.dumps(
            {  # 6. unknown disposition
                "event_id": "evt_003",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "+91 98765 43210",
                "payload": {"disposition_code": "INVALID_DISPO"},
            }
        ),
        json.dumps(
            {  # 7. valid disposition
                "event_id": "evt_004",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:06:00Z",
                "phone": "+91 98765 43210",
                "payload": {"disposition_code": "renewed"},
            }
        ),
    ]

    event_file = tmpdir / "events.jsonl"
    event_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    counters = DataQualityCounters()
    processor.process_event_file("drop_1", event_file, counters)

    assert counters.events_total == 7
    assert counters.malformed_events == 2  # lines 1 & 2
    assert counters.duplicate_events_dropped == 1  # line 4
    assert counters.unknown_disposition_events == 1  # line 6

    with db.get_connection() as conn:
        ledger_count = conn.execute(
            "SELECT COUNT(*) FROM interaction_ledger"
        ).fetchone()[0]
        # evt_001 and evt_004 are in ledger
        assert ledger_count == 2

        # evt_002 is in quarantined_events
        quar_count = conn.execute("SELECT COUNT(*) FROM quarantined_events").fetchone()[
            0
        ]
        assert quar_count == 1
        quar_event = conn.execute("SELECT event_id FROM quarantined_events").fetchone()
        assert quar_event["event_id"] == "evt_002"


def test_disposition_override_linking_without_phone(test_env):
    processor = test_env["processor"]
    lead_service = test_env["lead_service"]
    db = test_env["db"]
    tmpdir = test_env["tmpdir"]

    # Ingest lead
    (tmpdir / "batch_001.csv").write_text(
        "customer_name,phone,policy_number\nSneha Rao,+91 98765 43210,MOT/001\n",
        encoding="utf-8",
    )
    lead_service.ingest_lead_file(
        "drop_1", "motor_renewal", tmpdir / "batch_001.csv", DataQualityCounters()
    )

    # Call events: call.initiated, disposition.set, and disposition.override (NO phone)
    lines = [
        json.dumps(
            {
                "event_id": "evt_init",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_123",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
            }
        ),
        json.dumps(
            {
                "event_id": "evt_set",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "call_123",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "not_interested"},
            }
        ),
        json.dumps(
            {
                "event_id": "evt_override",
                "event_type": "disposition.override",
                "workflow_id": "motor_renewal",
                "call_id": "call_123",
                "occurred_at": "2026-10-05T14:00:00Z",
                "payload": {
                    "disposition_code": "renewed",
                    "overridden_by": "supervisor_1",
                },
                # Notice: NO phone field!
            }
        ),
    ]
    (tmpdir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    counters = DataQualityCounters()
    processor.process_event_file("drop_1", tmpdir / "events.jsonl", counters)

    with db.get_connection() as conn:
        override_row = conn.execute(
            "SELECT * FROM interaction_ledger WHERE event_id = 'evt_override'"
        ).fetchone()
        assert override_row is not None
        assert override_row["lead_id"] is not None
        assert override_row["call_id"] == "call_123"


def test_orphan_event_recovery(test_env):
    processor = test_env["processor"]
    lead_service = test_env["lead_service"]
    linking_service = test_env["linking_service"]
    db = test_env["db"]
    tmpdir = test_env["tmpdir"]

    # 1. Events arrive in drop_1 BEFORE the lead is uploaded!
    lines = [
        json.dumps(
            {
                "event_id": "evt_early",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_999",
                "occurred_at": "2026-10-01T10:00:00Z",
                "phone": "+91 91234 56789",
            }
        ),
    ]
    (tmpdir / "events_drop1.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    counters = DataQualityCounters()
    processor.process_event_file("drop_1", tmpdir / "events_drop1.jsonl", counters)

    # Event must be quarantined as orphan
    with db.get_connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM quarantined_events").fetchone()[0] == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM interaction_ledger").fetchone()[0] == 0
        )

    # 2. Lead arrives in drop_2
    (tmpdir / "batch_drop2.csv").write_text(
        "customer_name,phone,policy_number\nLate Customer,+91 91234 56789,MOT/LATE\n",
        encoding="utf-8",
    )
    lead_service.ingest_lead_file(
        "drop_2", "motor_renewal", tmpdir / "batch_drop2.csv", counters
    )

    # 3. Trigger relink_orphans
    with db.transaction() as conn:
        recovered = linking_service.relink_orphans(conn, counters)

    assert recovered == 1

    # Quarantined count is now 0; ledger count is now 1!
    with db.get_connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM quarantined_events").fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM interaction_ledger").fetchone()[0] == 1
        )
        ev = conn.execute(
            "SELECT * FROM interaction_ledger WHERE event_id = 'evt_early'"
        ).fetchone()
        assert ev["lead_id"] is not None
