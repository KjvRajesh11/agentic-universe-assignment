"""Event processor handling validation, deduplication, orphan detection, and ledger recording."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from solution.database import Database
from solution.disposition_service import DispositionService
from solution.event_linking import EventLinkingService
from solution.models import DataQualityCounters
from solution.normalisation import parse_timestamp_to_ist
from solution.pii_protection import PIIProtector

logger = logging.getLogger(__name__)


class EventProcessor:
    """Processes JSON Lines event streams in the strict specification order."""

    def __init__(
        self,
        db: Database,
        pii_protector: PIIProtector,
        disposition_service: DispositionService,
        linking_service: EventLinkingService,
    ) -> None:
        self.db = db
        self.pii_protector = pii_protector
        self.disposition_service = disposition_service
        self.linking_service = linking_service
        self._seen_event_ids: set[str] = set()
        self._load_existing_event_ids()

    def _load_existing_event_ids(self) -> None:
        """Cache existing event IDs from database to support fast deduplication."""
        with self.db.get_connection() as conn:
            rows1 = conn.execute("SELECT event_id FROM interaction_ledger").fetchall()
            for r in rows1:
                self._seen_event_ids.add(r["event_id"])
            rows2 = conn.execute("SELECT event_id FROM quarantined_events").fetchall()
            for r in rows2:
                self._seen_event_ids.add(r["event_id"])

    def process_event_file(
        self,
        drop_name: str,
        file_path: Path | str,
        counters: DataQualityCounters,
    ) -> None:
        """Process a single JSON Lines event file."""
        path = Path(file_path)

        with open(path, "rb") as f:
            content = f.read()
            file_hash = hashlib.sha256(content).hexdigest()

        with self.db.get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM event_file_ingestion_log
                WHERE drop_name = ? AND file_path = ?
                """,
                (drop_name, str(path)),
            ).fetchone()
            if existing:
                logger.info(
                    "Event file %s in %s already ingested, skipping.",
                    path.name,
                    drop_name,
                )
                return

        logger.info("Ingesting events from %s in %s", path.name, drop_name)

        text = content.decode("utf-8", errors="replace")
        lines = text.splitlines()

        file_total = 0
        file_malformed = 0
        file_duplicate = 0
        file_unknown_disp = 0

        with self.db.transaction() as conn:
            for raw_line in lines:
                line = raw_line.strip()
                if not line:
                    continue

                file_total += 1
                counters.events_total += 1

                # 1. Malformed check: valid JSON and has event_id
                try:
                    event_data = json.loads(line)
                    if not isinstance(event_data, dict):
                        file_malformed += 1
                        counters.malformed_events += 1
                        continue
                    event_id = event_data.get("event_id")
                    if not event_id or not isinstance(event_id, str):
                        file_malformed += 1
                        counters.malformed_events += 1
                        continue
                except (json.JSONDecodeError, TypeError, ValueError):
                    file_malformed += 1
                    counters.malformed_events += 1
                    continue

                # 2. Duplicate check: event_id already seen
                if event_id in self._seen_event_ids:
                    file_duplicate += 1
                    counters.duplicate_events_dropped += 1
                    continue

                self._seen_event_ids.add(event_id)

                event_type = event_data.get("event_type", "")
                workflow_id = event_data.get("workflow_id", "")
                call_id = event_data.get("call_id", "")
                occurred_at_raw = event_data.get("occurred_at", "")
                payload = event_data.get("payload") or {}

                try:
                    _, iso_ist, date_ist, epoch_sec = parse_timestamp_to_ist(
                        occurred_at_raw
                    )
                except ValueError:
                    # Invalid timestamp format treated as malformed
                    file_malformed += 1
                    counters.malformed_events += 1
                    continue

                # 3. Orphan check: resolve lead
                phone_raw = event_data.get("phone")
                lead_id: int | None = None
                phone_canonical: str | None = None
                phone_fingerprint: str | None = None

                if event_type == "disposition.override":
                    # Override has no phone, links via call_id
                    lead_id, phone_fingerprint = (
                        self.linking_service.find_lead_id_for_call(conn, call_id)
                    )
                else:
                    lead_id, phone_canonical, phone_fingerprint = (
                        self.linking_service.find_lead_id_for_event(
                            conn, workflow_id, phone_raw
                        )
                    )

                if lead_id is None:
                    # Event is an orphan -> quarantine
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO quarantined_events
                            (event_id, event_type, workflow_id, call_id, occurred_at,
                             occurred_at_epoch, ist_date, phone_canonical, phone_fingerprint,
                             payload_json, drop_name, reason, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'orphan_lead_not_found', datetime('now'))
                        """,
                        (
                            event_id,
                            event_type,
                            workflow_id,
                            call_id,
                            iso_ist,
                            epoch_sec,
                            date_ist,
                            phone_canonical,
                            phone_fingerprint,
                            json.dumps(payload),
                            drop_name,
                        ),
                    )
                    continue

                # 4. Unknown disposition check
                if event_type in ("disposition.set", "disposition.override"):
                    disp_code = payload.get("disposition_code")
                    if not disp_code or not self.disposition_service.is_valid_code(
                        workflow_id, disp_code
                    ):
                        file_unknown_disp += 1
                        counters.unknown_disposition_events += 1
                        # Unknown disposition: ignored from ledger
                        continue

                # 5. Valid event -> record in append-only interaction ledger
                conn.execute(
                    """
                    INSERT INTO interaction_ledger
                        (event_id, event_type, workflow_id, call_id, occurred_at,
                         occurred_at_epoch, ist_date, lead_id, phone_fingerprint,
                         payload_json, drop_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        iso_ist,
                        epoch_sec,
                        date_ist,
                        lead_id,
                        phone_fingerprint,
                        json.dumps(payload),
                        drop_name,
                    ),
                )
                self.linking_service.register_call_link(
                    call_id, lead_id, phone_fingerprint
                )

            # Record event file log
            conn.execute(
                """
                INSERT OR REPLACE INTO event_file_ingestion_log
                    (drop_name, file_path, file_hash, total_lines, malformed_lines,
                     duplicate_lines, unknown_disposition_lines, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    drop_name,
                    str(path),
                    file_hash,
                    file_total,
                    file_malformed,
                    file_duplicate,
                    file_unknown_disp,
                ),
            )
