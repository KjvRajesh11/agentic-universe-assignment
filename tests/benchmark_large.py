"""Performance benchmark runner for Assignment A."""

import time
from pathlib import Path

from solution.config import AppConfig
from solution.main import PipelineOrchestrator


def run_benchmark():
    db_path = Path("large_bench.db")
    reports_dir = Path("reports_large")
    if db_path.exists():
        db_path.unlink()

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

    t_start = time.perf_counter()

    # Drop 1
    t0 = time.perf_counter()
    orchestrator.ingest_drop("data_large/drop_1")
    t_drop1 = time.perf_counter() - t0
    print(f"Drop 1 Ingestion Time: {t_drop1:.2f}s")

    # Drop 2
    t0 = time.perf_counter()
    orchestrator.ingest_drop("data_large/drop_2")
    t_drop2 = time.perf_counter() - t0
    print(f"Drop 2 Ingestion Time: {t_drop2:.2f}s")

    # Reporting
    t0 = time.perf_counter()
    orchestrator.generate_reports(reports_dir)
    t_rep = time.perf_counter() - t0
    print(f"Reporting Generation Time: {t_rep:.2f}s")

    t_total = time.perf_counter() - t_start
    db_size_mb = db_path.stat().st_size / (1024 * 1024)

    print("=" * 40)
    print("LARGE DATASET BENCHMARK RESULTS")
    print("=" * 40)
    print(f"Drop 1 Duration:    {t_drop1:.2f} s")
    print(f"Drop 2 Duration:    {t_drop2:.2f} s")
    print(f"Reports Duration:   {t_rep:.2f} s")
    print(f"Total Pipeline:     {t_total:.2f} s")
    print(f"Final DB Size:      {db_size_mb:.2f} MB")
    print("=" * 40)

    # Clean up benchmark artifacts
    if db_path.exists():
        db_path.unlink()
    for wal in Path(".").glob("large_bench.db*"):
        wal.unlink(missing_ok=True)
    import shutil

    if reports_dir.exists():
        shutil.rmtree(reports_dir)


if __name__ == "__main__":
    run_benchmark()
