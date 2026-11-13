"""Report generation service implementing exact output contracts and atomic file replacement."""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from solution.bucket_service import BucketService
from solution.database import Database
from solution.disposition_service import DispositionService
from solution.schema_loader import SchemaLoader

logger = logging.getLogger(__name__)


class ReportGenerator:
    """Generates the five required reports according to strict output contracts."""

    def __init__(
        self,
        db: Database,
        schema_loader: SchemaLoader,
        disposition_service: DispositionService,
    ) -> None:
        self.db = db
        self.schema_loader = schema_loader
        self.disposition_service = disposition_service

    def generate_all_reports(self, output_dir: Path | str) -> dict[str, Path]:
        """Generate all five reports in output_dir atomically."""
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        logger.info("Generating reports into %s", out_path)

        rep1 = self.generate_batch_summary(out_path / "batch_summary.csv")
        rep2 = self.generate_daily_overview(out_path / "daily_overview.csv")
        rep3 = self.generate_lead_report(out_path / "lead_report.csv")
        rep4 = self.generate_rejections_report(out_path / "rejections.csv")
        rep5 = self.generate_data_quality(out_path / "data_quality.json")

        logger.info("Successfully generated all five reports.")
        return {
            "batch_summary": rep1,
            "daily_overview": rep2,
            "lead_report": rep3,
            "rejections": rep4,
            "data_quality": rep5,
        }

    def _atomic_write_csv(
        self, target_path: Path, header: list[str], rows: list[list[Any]]
    ) -> Path:
        """Atomically write CSV file."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(
            dir=str(target_path.parent), prefix=".tmp_rep_"
        )
        try:
            with os.fdopen(temp_fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
                writer.writerow(header)
                for r in rows:
                    writer.writerow(r)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, str(target_path))
        except Exception:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            raise
        return target_path

    def _atomic_write_json(self, target_path: Path, data: dict[str, Any]) -> Path:
        """Atomically write formatted JSON file."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(
            dir=str(target_path.parent), prefix=".tmp_rep_"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, str(target_path))
        except Exception:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            raise
        return target_path

    def _aggregate_calls_and_leads(
        self,
    ) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
        """
        Aggregate ledger events into calls, then evaluate lead stats and buckets.
        Returns:
            lead_stats: dict mapping lead_id -> lead aggregate info
            all_calls: list of all valid calls with metadata
        """
        with self.db.get_connection() as conn:
            # 1. Fetch all leads
            leads_rows = conn.execute(
                """
                SELECT id, workflow_id, external_ref, batch_id, phone_masked
                FROM leads
                ORDER BY workflow_id, external_ref
                """
            ).fetchall()

            lead_stats: dict[int, dict[str, Any]] = {}
            for row in leads_rows:
                lead_stats[row["id"]] = {
                    "lead_id": row["id"],
                    "workflow_id": row["workflow_id"],
                    "external_ref": row["external_ref"],
                    "batch_id": row["batch_id"],
                    "phone_masked": row["phone_masked"],
                    "calls": [],
                }

            # 2. Fetch all ledger events grouped by call_id
            events_rows = conn.execute(
                """
                SELECT event_id, event_type, workflow_id, call_id, occurred_at,
                       occurred_at_epoch, ist_date, lead_id, payload_json
                FROM interaction_ledger
                ORDER BY call_id, occurred_at_epoch ASC
                """
            ).fetchall()

        events_by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in events_rows:
            events_by_call[r["call_id"]].append(
                {
                    "event_id": r["event_id"],
                    "event_type": r["event_type"],
                    "workflow_id": r["workflow_id"],
                    "call_id": r["call_id"],
                    "occurred_at": r["occurred_at"],
                    "occurred_at_epoch": r["occurred_at_epoch"],
                    "ist_date": r["ist_date"],
                    "lead_id": r["lead_id"],
                    "payload": json.loads(r["payload_json"]),
                }
            )

        all_calls: list[dict[str, Any]] = []

        # Process each call
        for call_id, c_events in events_by_call.items():
            # A call is counted ONLY if its call.initiated event is present (and linked)
            init_event = next(
                (e for e in c_events if e["event_type"] == "call.initiated"), None
            )
            if not init_event:
                continue

            workflow_id = init_event["workflow_id"]
            lead_id = init_event["lead_id"]
            initiated_at = init_event["occurred_at"]
            initiated_epoch = init_event["occurred_at_epoch"]
            ist_date = init_event["ist_date"]

            is_connected = any(e["event_type"] == "call.connected" for e in c_events)
            is_telephony_failed = any(
                e["event_type"] == "call.telephony_failed" for e in c_events
            )

            eff_code, eff_disp = self.disposition_service.resolve_effective_disposition(
                workflow_id, c_events
            )
            is_terminal = eff_disp.is_terminal if eff_disp else False
            is_success = eff_disp.is_success if eff_disp else False

            call_info = {
                "call_id": call_id,
                "workflow_id": workflow_id,
                "lead_id": lead_id,
                "initiated_at": initiated_at,
                "initiated_epoch": initiated_epoch,
                "ist_date": ist_date,
                "is_connected": is_connected,
                "is_telephony_failed": is_telephony_failed,
                "effective_disposition": eff_code,
                "is_terminal": is_terminal,
                "is_success": is_success,
            }
            all_calls.append(call_info)

            if lead_id in lead_stats:
                lead_stats[lead_id]["calls"].append(call_info)

        # Classify each lead
        for l_data in lead_stats.values():
            l_calls = l_data["calls"]
            total_calls = len(l_calls)
            connected_calls = sum(1 for c in l_calls if c["is_connected"])
            has_terminal = any(c["is_terminal"] for c in l_calls)
            every_failed = (total_calls > 0) and all(
                c["is_telephony_failed"] for c in l_calls
            )

            bucket = BucketService.classify_lead_bucket(
                total_calls, has_terminal, every_failed
            )

            # Sort calls by initiated_epoch descending
            l_calls_sorted = sorted(
                l_calls, key=lambda c: c["initiated_epoch"], reverse=True
            )
            last_call_at = l_calls_sorted[0]["initiated_at"] if l_calls_sorted else ""

            # Current disposition: effective disposition of the most recent call (by initiated time) that has one
            current_disp = ""
            for c in l_calls_sorted:
                if c["effective_disposition"]:
                    current_disp = c["effective_disposition"]
                    break

            ever_success = any(c["is_success"] for c in l_calls)

            l_data["bucket"] = bucket
            l_data["total_calls"] = total_calls
            l_data["connected_calls"] = connected_calls
            l_data["last_call_at"] = last_call_at
            l_data["current_disposition"] = current_disp
            l_data["ever_success"] = "true" if ever_success else "false"

        return lead_stats, all_calls

    def generate_batch_summary(self, target_path: Path) -> Path:
        """
        1. batch_summary.csv
        workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed
        Sorted by workflow_id, batch_id
        """
        lead_stats, _ = self._aggregate_calls_and_leads()

        # Group by (workflow_id, batch_id)
        grouped: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {
                "total_leads": 0,
                "yet_to_reach_out": 0,
                "in_progress_telephony_failed": 0,
                "in_progress_dialed": 0,
                "completed": 0,
            }
        )

        for l_data in lead_stats.values():
            key = (l_data["workflow_id"], l_data["batch_id"])
            grouped[key]["total_leads"] += 1
            bucket = l_data["bucket"]
            if bucket in grouped[key]:
                grouped[key][bucket] += 1

        rows: list[list[Any]] = []
        for wf, b_id in sorted(grouped.keys()):
            cnts = grouped[(wf, b_id)]
            rows.append(
                [
                    wf,
                    b_id,
                    cnts["total_leads"],
                    cnts["yet_to_reach_out"],
                    cnts["in_progress_telephony_failed"],
                    cnts["in_progress_dialed"],
                    cnts["completed"],
                ]
            )

        header = [
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ]
        return self._atomic_write_csv(target_path, header, rows)

    def generate_daily_overview(self, target_path: Path) -> Path:
        """
        2. daily_overview.csv
        one row per workflow per IST date that has at least one call, sorted by workflow_id, date
        workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads
        """
        _, all_calls = self._aggregate_calls_and_leads()

        # Group by (workflow_id, date)
        daily_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for c in all_calls:
            daily_groups[(c["workflow_id"], c["ist_date"])].append(c)

        rows: list[list[Any]] = []
        for wf, dt in sorted(daily_groups.keys()):
            calls_on_date = daily_groups[(wf, dt)]
            total_calls = len(calls_on_date)
            connected = sum(1 for c in calls_on_date if c["is_connected"])
            rate = connected / total_calls if total_calls > 0 else 0.0
            rate_str = f"{rate:.4f}"

            # Distinct leads with at least one call on that date whose effective disposition has is_success=true
            succ_leads = {c["lead_id"] for c in calls_on_date if c["is_success"]}
            success_count = len(succ_leads)

            rows.append([wf, dt, total_calls, connected, rate_str, success_count])

        header = [
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ]
        return self._atomic_write_csv(target_path, header, rows)

    def generate_lead_report(self, target_path: Path) -> Path:
        """
        3. lead_report.csv
        one row per lead, sorted by workflow_id, external_ref
        workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success
        """
        lead_stats, _ = self._aggregate_calls_and_leads()

        # Sort by workflow_id, external_ref
        sorted_leads = sorted(
            lead_stats.values(),
            key=lambda l: (l["workflow_id"], l["external_ref"]),
        )

        rows: list[list[Any]] = []
        for l in sorted_leads:
            rows.append(
                [
                    l["workflow_id"],
                    l["external_ref"],
                    l["batch_id"],
                    l["phone_masked"],
                    l["bucket"],
                    l["total_calls"],
                    l["connected_calls"],
                    l["last_call_at"],
                    l["current_disposition"],
                    l["ever_success"],
                ]
            )

        header = [
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
        return self._atomic_write_csv(target_path, header, rows)

    def generate_rejections_report(self, target_path: Path) -> Path:
        """
        4. rejections.csv
        sorted by workflow_id, batch_id, row_number. No PII.
        workflow_id,batch_id,row_number,reason
        """
        with self.db.get_connection() as conn:
            rejections = conn.execute(
                """
                SELECT workflow_id, batch_id, row_number, reason
                FROM rejected_leads
                ORDER BY workflow_id, batch_id, row_number
                """
            ).fetchall()

        rows = [
            [r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]]
            for r in rejections
        ]
        header = ["workflow_id", "batch_id", "row_number", "reason"]
        return self._atomic_write_csv(target_path, header, rows)

    def generate_data_quality(self, target_path: Path) -> Path:
        """
        5. data_quality.json
        Exact prescribed structure.
        """
        with self.db.get_connection() as conn:
            # Batch metrics
            b_row = conn.execute(
                """
                SELECT COALESCE(SUM(non_blank_rows), 0) AS total_rows,
                       COALESCE(SUM(merged_duplicates), 0) AS merged_dups,
                       COALESCE(SUM(field_warnings), 0) AS warnings
                FROM batch_ingestion_log
                """
            ).fetchone()
            total_lead_rows = int(b_row["total_rows"] if b_row else 0)
            merged_dups = int(b_row["merged_dups"] if b_row else 0)
            field_warnings = int(b_row["warnings"] if b_row else 0)

            # Rejections
            rej_rows = conn.execute(
                """
                SELECT reason, COUNT(*) as cnt
                FROM rejected_leads
                GROUP BY reason
                ORDER BY reason
                """
            ).fetchall()
            rejections_by_reason: dict[str, int] = {
                r["reason"]: r["cnt"] for r in rej_rows
            }
            total_rejected = sum(rejections_by_reason.values())

            # Event metrics
            e_row = conn.execute(
                """
                SELECT COALESCE(SUM(total_lines), 0) AS total_lines,
                       COALESCE(SUM(malformed_lines), 0) AS malformed,
                       COALESCE(SUM(duplicate_lines), 0) AS duplicates,
                       COALESCE(SUM(unknown_disposition_lines), 0) AS unknown_disp
                FROM event_file_ingestion_log
                """
            ).fetchone()
            events_total = int(e_row["total_lines"] if e_row else 0)
            malformed_events = int(e_row["malformed"] if e_row else 0)
            duplicate_events_dropped = int(e_row["duplicates"] if e_row else 0)
            unknown_disp_events = int(e_row["unknown_disp"] if e_row else 0)

            # Orphan count reflects final state
            orphan_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM quarantined_events"
            ).fetchone()
            orphan_events = int(orphan_row["cnt"] if orphan_row else 0)

        data = {
            "lead_rows_total": total_lead_rows,
            "lead_rows_rejected": total_rejected,
            "rejections_by_reason": rejections_by_reason,
            "duplicate_lead_rows_merged": merged_dups,
            "field_warnings": field_warnings,
            "events_total": events_total,
            "malformed_events": malformed_events,
            "duplicate_events_dropped": duplicate_events_dropped,
            "orphan_events": orphan_events,
            "unknown_disposition_events": unknown_disp_events,
        }
        return self._atomic_write_json(target_path, data)
