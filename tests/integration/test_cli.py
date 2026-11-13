"""Integration tests for the CLI commands."""

import tempfile
from pathlib import Path

import pytest

from solution.main import main


def test_cli_help():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_full_cycle(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_file = tmp_path / "test.db"
        reports_dir = tmp_path / "reports"

        monkeypatch.setenv(
            "APP_ENCRYPTION_KEY",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        monkeypatch.setenv(
            "APP_HMAC_SECRET", "secret_hmac_key_for_testing_purposes_only_32_bytes!"
        )

        # Create dummy drop
        drop_dir = tmp_path / "drop_1"
        leads_dir = drop_dir / "leads" / "policy_feedback"
        leads_dir.mkdir(parents=True, exist_ok=True)
        (leads_dir / "batch_001.csv").write_text(
            "customer_name,phone,order_id\nFarhan Khan,+91 98765 43210,ORD-101\n",
            encoding="utf-8",
        )

        events_dir = drop_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events_0001.jsonl").write_text(
            '{"event_id": "evt_cli", "event_type": "call.initiated", "workflow_id": "policy_feedback", "call_id": "call_cli", "occurred_at": "2026-10-05T12:00:00Z", "phone": "9876543210"}\n',
            encoding="utf-8",
        )

        # Run ingest
        exit_code_ingest = main(["--db", str(db_file), "ingest", str(drop_dir)])
        assert exit_code_ingest == 0

        # Run report
        exit_code_report = main(
            ["--db", str(db_file), "report", "--out", str(reports_dir)]
        )
        assert exit_code_report == 0

        assert (reports_dir / "batch_summary.csv").exists()
        assert (reports_dir / "daily_overview.csv").exists()
        assert (reports_dir / "lead_report.csv").exists()
        assert (reports_dir / "rejections.csv").exists()
        assert (reports_dir / "data_quality.json").exists()
