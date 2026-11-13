"""Event-to-lead linking and orphan event recovery service."""

from __future__ import annotations

import json
import logging
from typing import Any

from solution.disposition_service import DispositionService
from solution.models import DataQualityCounters
from solution.normalisation import normalise_phone
from solution.pii_protection import PIIProtector

logger = logging.getLogger(__name__)


class EventLinkingService:
    """Manages linking of interaction events to leads and relinking quarantined orphans."""

    def __init__(
        self, pii_protector: PIIProtector, disposition_service: DispositionService
    ) -> None:
        self.pii_protector = pii_protector
        self.disposition_service = disposition_service
        # In-memory caches for fast resolution: (workflow_id, phone_fingerprint) -> lead_id
        self._lead_cache: dict[tuple[str, str], int] = {}
        # call_id -> (lead_id, phone_fingerprint)
        self._call_lead_cache: dict[str, tuple[int, str | None]] = {}

    def register_lead(
        self, workflow_id: str, phone_fingerprint: str, lead_id: int
    ) -> None:
        """Register a newly inserted or loaded lead into the memory cache."""
        self._lead_cache[(workflow_id, phone_fingerprint)] = lead_id

    def register_call_link(
        self, call_id: str, lead_id: int, phone_fingerprint: str | None
    ) -> None:
        """Register a call-to-lead link in the memory cache."""
        self._call_lead_cache[call_id] = (lead_id, phone_fingerprint)

    def find_lead_id_for_event(
        self,
        conn: Any,
        workflow_id: str,
        phone_raw: str | None,
    ) -> tuple[int | None, str | None, str | None]:
        """
        Attempt to resolve lead_id from phone:
        Returns (lead_id, canonical_phone, phone_fingerprint).
        """
        if not phone_raw:
            return None, None, None

        canonical_phone, is_valid = normalise_phone(phone_raw)
        if not is_valid or not canonical_phone:
            return None, None, None

        phone_fingerprint = self.pii_protector.compute_phone_fingerprint(
            canonical_phone
        )
        cache_key = (workflow_id, phone_fingerprint)

        if cache_key in self._lead_cache:
            return self._lead_cache[cache_key], canonical_phone, phone_fingerprint

        row = conn.execute(
            "SELECT id FROM leads WHERE workflow_id = ? AND phone_fingerprint = ?",
            (workflow_id, phone_fingerprint),
        ).fetchone()

        if row:
            l_id = row["id"]
            self._lead_cache[cache_key] = l_id
            return l_id, canonical_phone, phone_fingerprint

        return None, canonical_phone, phone_fingerprint

    def find_lead_id_for_call(
        self, conn: Any, call_id: str
    ) -> tuple[int | None, str | None]:
        """
        Resolve lead_id for a call from previously linked interaction events.
        Used for disposition.override events which do not contain a phone number.
        """
        if call_id in self._call_lead_cache:
            return self._call_lead_cache[call_id]

        row = conn.execute(
            """
            SELECT lead_id, phone_fingerprint
            FROM interaction_ledger
            WHERE call_id = ? AND lead_id IS NOT NULL
            LIMIT 1
            """,
            (call_id,),
        ).fetchone()

        if row:
            res = (row["lead_id"], row["phone_fingerprint"])
            self._call_lead_cache[call_id] = res
            return res
        return None, None

    def relink_orphans(self, conn: Any, counters: DataQualityCounters) -> int:
        """
        Replay and link quarantined orphan events whose leads have now been ingested.
        Iterates until no more orphans can be linked.
        Returns the number of recovered events.
        """
        total_recovered = 0

        while True:
            recovered_in_pass = 0

            # 1. Recover phone-based orphans
            phone_orphans = conn.execute(
                """
                SELECT q.id, q.event_id, q.event_type, q.workflow_id, q.call_id,
                       q.occurred_at, q.occurred_at_epoch, q.ist_date,
                       q.phone_canonical, q.phone_fingerprint, q.payload_json, q.drop_name
                FROM quarantined_events q
                JOIN leads l ON q.workflow_id = l.workflow_id AND q.phone_fingerprint = l.phone_fingerprint
                WHERE q.phone_fingerprint IS NOT NULL
                """
            ).fetchall()

            for row in phone_orphans:
                # Check lead_id
                lead_row = conn.execute(
                    "SELECT id FROM leads WHERE workflow_id = ? AND phone_fingerprint = ?",
                    (row["workflow_id"], row["phone_fingerprint"]),
                ).fetchone()
                if not lead_row:
                    continue

                lead_id = lead_row["id"]
                payload = json.loads(row["payload_json"])

                # Check if this was a disposition with an unknown code
                if row["event_type"] in ("disposition.set", "disposition.override"):
                    code = payload.get("disposition_code")
                    if not code or not self.disposition_service.is_valid_code(
                        row["workflow_id"], code
                    ):
                        # Unknown disposition: discard from quarantine and count
                        counters.unknown_disposition_events += 1
                        conn.execute(
                            "DELETE FROM quarantined_events WHERE id = ?", (row["id"],)
                        )
                        recovered_in_pass += 1
                        continue

                # Move into interaction_ledger
                conn.execute(
                    """
                    INSERT INTO interaction_ledger
                        (event_id, event_type, workflow_id, call_id, occurred_at,
                         occurred_at_epoch, ist_date, lead_id, phone_fingerprint,
                         payload_json, drop_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        row["event_id"],
                        row["event_type"],
                        row["workflow_id"],
                        row["call_id"],
                        row["occurred_at"],
                        row["occurred_at_epoch"],
                        row["ist_date"],
                        lead_id,
                        row["phone_fingerprint"],
                        row["payload_json"],
                        row["drop_name"],
                    ),
                )
                conn.execute(
                    "DELETE FROM quarantined_events WHERE id = ?", (row["id"],)
                )
                recovered_in_pass += 1
                total_recovered += 1

            # 2. Recover override orphans (which link by call_id)
            call_orphans = conn.execute(
                """
                SELECT q.id, q.event_id, q.event_type, q.workflow_id, q.call_id,
                       q.occurred_at, q.occurred_at_epoch, q.ist_date,
                       q.payload_json, q.drop_name
                FROM quarantined_events q
                WHERE q.event_type = 'disposition.override'
                """
            ).fetchall()

            for row in call_orphans:
                lead_id, phone_fp = self.find_lead_id_for_call(conn, row["call_id"])
                if lead_id is None:
                    continue

                payload = json.loads(row["payload_json"])
                code = payload.get("disposition_code")
                if not code or not self.disposition_service.is_valid_code(
                    row["workflow_id"], code
                ):
                    counters.unknown_disposition_events += 1
                    conn.execute(
                        "DELETE FROM quarantined_events WHERE id = ?", (row["id"],)
                    )
                    recovered_in_pass += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO interaction_ledger
                        (event_id, event_type, workflow_id, call_id, occurred_at,
                         occurred_at_epoch, ist_date, lead_id, phone_fingerprint,
                         payload_json, drop_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        row["event_id"],
                        row["event_type"],
                        row["workflow_id"],
                        row["call_id"],
                        row["occurred_at"],
                        row["occurred_at_epoch"],
                        row["ist_date"],
                        lead_id,
                        phone_fp,
                        row["payload_json"],
                        row["drop_name"],
                    ),
                )
                conn.execute(
                    "DELETE FROM quarantined_events WHERE id = ?", (row["id"],)
                )
                recovered_in_pass += 1
                total_recovered += 1

            if recovered_in_pass == 0:
                break

        if total_recovered > 0:
            logger.info("Successfully recovered %d orphaned events.", total_recovered)

        return total_recovered
