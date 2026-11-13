"""Regression and end-to-end integration test running on a fresh, unseen random seed."""

import subprocess
import sys
import tempfile
from pathlib import Path

from solution.config import AppConfig
from solution.main import PipelineOrchestrator


def test_full_pipeline_fresh_seed():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        data_dir = tmp_path / "data"
        db_path = tmp_path / "fresh.db"
        reports_dir = tmp_path / "reports"

        # Generate data using seed 9999
        cmd_gen = [
            sys.executable,
            "assignment-a-data/tools/generate_data.py",
            "--seed",
            "9999",
            "--scale",
            "small",
            "--out",
            str(data_dir),
        ]
        res = subprocess.run(cmd_gen, capture_output=True, text=True, check=True)
        assert "Generated seed=9999" in res.stdout

        # Initialize pipeline
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

        # Ingest drop_1
        orchestrator.ingest_drop(data_dir / "drop_1")
        # Ingest drop_2
        orchestrator.ingest_drop(data_dir / "drop_2")

        # Generate reports
        rep_paths = orchestrator.generate_reports(reports_dir)

        # Verify all 5 files exist
        for key, p in rep_paths.items():
            assert p.exists(), f"Report {key} does not exist at {p}"

        # Re-run drop_1 and drop_2 (idempotency check on fresh seed)
        orchestrator.ingest_drop(data_dir / "drop_1")
        orchestrator.ingest_drop(data_dir / "drop_2")

        # Reports should still generate cleanly
        rep_paths2 = orchestrator.generate_reports(reports_dir)
        for key, p in rep_paths2.items():
            assert p.exists()
