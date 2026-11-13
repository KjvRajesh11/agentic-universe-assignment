"""CLI entrypoint and orchestration for the interaction ledger and reporting pipeline."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from solution.config import AppConfig
from solution.database import Database
from solution.disposition_service import DispositionService
from solution.event_linking import EventLinkingService
from solution.event_processor import EventProcessor
from solution.exceptions import ConfigurationError, PipelineError
from solution.lead_ingestion import LeadIngestionService
from solution.logging_config import configure_logging
from solution.models import DataQualityCounters
from solution.pii_protection import PIIProtector
from solution.reporting import ReportGenerator
from solution.schema_loader import SchemaLoader

logger = logging.getLogger("solution")


def load_dotenv(env_path: Path | str = ".env") -> None:
    """Lightweight .env parser without external dependencies."""
    p = Path(env_path)
    if not p.is_file():
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, v = stripped.split("=", 1)
                k_clean = k.strip()
                v_clean = v.strip().strip("'\"")
                if k_clean and k_clean not in os.environ:
                    os.environ[k_clean] = v_clean
    except OSError as e:
        logger.warning("Could not read .env file: %s", e)


class PipelineOrchestrator:
    """Orchestrates drop ingestion and reporting."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.schema_loader = SchemaLoader(config.schema_dir, config.dispositions_path)
        self.pii_protector = PIIProtector(config.encryption_key, config.hmac_secret)
        self.disposition_service = DispositionService(self.schema_loader)
        self.linking_service = EventLinkingService(
            self.pii_protector, self.disposition_service
        )
        self.lead_service = LeadIngestionService(
            self.db, self.schema_loader, self.pii_protector, self.linking_service
        )
        self.event_processor = EventProcessor(
            self.db,
            self.pii_protector,
            self.disposition_service,
            self.linking_service,
        )
        self.reporter = ReportGenerator(
            self.db, self.schema_loader, self.disposition_service
        )

    def ingest_drop(self, drop_path: Path | str) -> DataQualityCounters:
        """
        Ingest a drop directory containing leads and events:
        - Checks idempotency: if already completed, returns immediately.
        - Ingests leads across workflows and batches in deterministic order.
        - Relinks existing quarantined orphans against newly arrived leads.
        - Ingests events in deterministic order.
        - Relinks any orphans again.
        - Marks drop completed.
        """
        path = Path(drop_path).resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Drop directory does not exist: {path}")

        drop_name = path.name  # e.g., 'drop_1'
        logger.info("Starting ingestion for %s from %s", drop_name, path)

        if self.db.is_drop_completed(drop_name):
            logger.info(
                "Drop %s has already been completely ingested. Idempotent skip.",
                drop_name,
            )
            return DataQualityCounters()

        now_iso = datetime.now(UTC).isoformat()
        self.db.record_drop_start(drop_name, str(path), now_iso)

        counters = DataQualityCounters()

        # 1. Ingest Leads
        leads_dir = path / "leads"
        if leads_dir.is_dir():
            # Iterate through workflow directories in sorted order
            for wf_dir in sorted(leads_dir.iterdir()):
                if not wf_dir.is_dir():
                    continue
                wf_id = wf_dir.name
                # Sort batch files by filename (e.g. batch_001.csv, batch_002.csv)
                for batch_file in sorted(wf_dir.glob("*.csv")):
                    self.lead_service.ingest_lead_file(
                        drop_name, wf_id, batch_file, counters
                    )

        # 2. Relink any previously orphaned events that now have matching leads
        with self.db.transaction() as conn:
            self.linking_service.relink_orphans(conn, counters)

        # 3. Ingest Events
        events_dir = path / "events"
        if events_dir.is_dir():
            for event_file in sorted(events_dir.glob("*.jsonl")):
                self.event_processor.process_event_file(drop_name, event_file, counters)

        # 4. Relink orphans again (e.g., if override arrived or subsequent relations resolved)
        with self.db.transaction() as conn:
            self.linking_service.relink_orphans(conn, counters)

        # 5. Mark drop completed
        done_iso = datetime.now(UTC).isoformat()
        self.db.record_drop_complete(drop_name, done_iso)

        logger.info("Completed ingestion for %s.", drop_name)
        return counters

    def generate_reports(self, output_dir: Path | str) -> dict[str, Path]:
        """Generate all five output reports."""
        return self.reporter.generate_all_reports(output_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI command line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m solution.main",
        description="Agentic Universe Interaction Ledger & Reporting Pipeline",
    )
    parser.add_argument("--db", help="Path to SQLite database (default: agentic.db)")
    parser.add_argument(
        "--schema-dir",
        help="Path to schema directory (default: assignment-a-data/schemas)",
    )
    parser.add_argument("--dispositions", help="Path to dispositions.csv catalogue")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose DEBUG logging"
    )
    parser.add_argument(
        "--dev-keys",
        action="store_true",
        help="Use deterministic fallback keys for local development if not configured in environment",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop directory")
    ingest_parser.add_argument(
        "drop_path", help="Path to drop directory (e.g. data/drop_1)"
    )

    # Report command
    report_parser = subparsers.add_parser("report", help="Generate all reports")
    report_parser.add_argument(
        "--out",
        default="reports",
        help="Directory to output generated reports (default: reports)",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint."""
    load_dotenv()
    args = parse_args(argv)

    log_level = "DEBUG" if args.verbose else "INFO"
    configure_logging(log_level)

    # Fallback dev keys if requested and not in environment
    enc_key = os.getenv("APP_ENCRYPTION_KEY")
    hmac_sec = os.getenv("APP_HMAC_SECRET")
    if args.dev_keys:
        if not enc_key:
            enc_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            logger.warning("Using development fallback encryption key via --dev-keys.")
        if not hmac_sec:
            hmac_sec = "development_secret_hmac_key_for_testing_purposes_only_32_bytes!"
            logger.warning("Using development fallback HMAC secret via --dev-keys.")

    try:
        config = AppConfig.from_env(
            db_path=args.db,
            schema_dir=args.schema_dir,
            dispositions_path=args.dispositions,
            encryption_key=enc_key,
            hmac_secret=hmac_sec,
            log_level=log_level,
        )
    except ConfigurationError as e:
        logger.error("Configuration error: %s", e)
        logger.error(
            "Please configure APP_ENCRYPTION_KEY and APP_HMAC_SECRET in environment or .env file, "
            "or run with --dev-keys for local development."
        )
        return 1

    orchestrator = PipelineOrchestrator(config)

    try:
        if args.command == "ingest":
            orchestrator.ingest_drop(args.drop_path)
            return 0
        elif args.command == "report":
            orchestrator.generate_reports(args.out)
            return 0
        else:
            logger.error("Unknown command: %s", args.command)
            return 1
    except PipelineError as e:
        logger.error("Pipeline failure: %s", e)
        return 1
    except Exception:
        logger.exception("Unexpected error occurred")
        return 2


if __name__ == "__main__":
    sys.exit(main())
