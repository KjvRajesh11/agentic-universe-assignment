"""Integration tests for idempotency: re-running ingestion must change nothing."""

import json
import tempfile
from pathlib import Path

import pytest

from solution.config import AppConfig
from solution.main import PipelineOrchestrator


@pytest.fixture
def clean_pipeline():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "agentic.db"
        reports_dir = tmp_path / "reports"
        enc_key = b"0123456789abcdef0123456789abcdef"
        hmac_key = b"hmac_secret_key_at_least_16_bytes_long"
        config = AppConfig(
            db_path=db_path,
            schema_dir=Path("assignment-a-data/schemas"),
            dispositions_path=Path("assignment-a-data/schemas/dispositions.csv"),
            encryption_key=enc_key,
            hmac_secret=hmac_key,
        )
        orchestrator = PipelineOrchestrator(config)
        yield {
            "orchestrator": orchestrator,
            "tmp_path": tmp_path,
            "reports_dir": reports_dir,
            "db_path": db_path,
        }


def test_drop_reingestion_idempotency(clean_pipeline):
    orchestrator = clean_pipeline["orchestrator"]
    tmp_path = clean_pipeline["tmp_path"]
    reports_dir = clean_pipeline["reports_dir"]

    # Construct synthetic drop_1
    drop_1 = tmp_path / "data" / "drop_1"
    leads_dir = drop_1 / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True, exist_ok=True)
    events_dir = drop_1 / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    csv_leads = (
        "customer_name,phone,policy_number,premium_amount\n"
        "Aarav Sharma,+91 98765 43210,MOT/001,12000\n"
        "Diya Patel,+91 91234 56789,MOT/002,15000\n"
    )
    (leads_dir / "batch_001.csv").write_text(csv_leads, encoding="utf-8")

    event_lines = [
        json.dumps(
            {
                "event_id": "evt_001",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "+91 98765 43210",
            }
        ),
        json.dumps(
            {
                "event_id": "evt_002",
                "event_type": "call.connected",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:10Z",
                "phone": "+91 98765 43210",
            }
        ),
    ]
    (events_dir / "events_0001.jsonl").write_text(
        "\n".join(event_lines) + "\n", encoding="utf-8"
    )

    # Ingest 1st time
    orchestrator.ingest_drop(drop_1)
    orchestrator.generate_reports(reports_dir)

    # Read reports after first run
    rep_batch_1 = (reports_dir / "batch_summary.csv").read_text(encoding="utf-8")
    rep_lead_1 = (reports_dir / "lead_report.csv").read_text(encoding="utf-8")
    rep_dq_1 = (reports_dir / "data_quality.json").read_text(encoding="utf-8")

    with orchestrator.db.get_connection() as conn:
        lead_count_1 = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        event_count_1 = conn.execute(
            "SELECT COUNT(*) FROM interaction_ledger"
        ).fetchone()[0]

    assert lead_count_1 == 2
    assert event_count_1 == 2

    # Ingest 2nd time (re-ingest same drop)
    orchestrator.ingest_drop(drop_1)
    orchestrator.generate_reports(reports_dir)

    # Verify database counts have NOT duplicated
    with orchestrator.db.get_connection() as conn:
        lead_count_2 = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        event_count_2 = conn.execute(
            "SELECT COUNT(*) FROM interaction_ledger"
        ).fetchone()[0]

    assert lead_count_2 == lead_count_1
    assert event_count_2 == event_count_1

    # Verify reports are byte-identical
    rep_batch_2 = (reports_dir / "batch_summary.csv").read_text(encoding="utf-8")
    rep_lead_2 = (reports_dir / "lead_report.csv").read_text(encoding="utf-8")
    rep_dq_2 = (reports_dir / "data_quality.json").read_text(encoding="utf-8")

    assert rep_batch_1 == rep_batch_2
    assert rep_lead_1 == rep_lead_2
    assert rep_dq_1 == rep_dq_2
