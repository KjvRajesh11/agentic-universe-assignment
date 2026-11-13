"""Lead ingestion service with schema validation, PII encryption, rejection handling, and duplicate merging."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solution.database import Database
from solution.models import (
    DataQualityCounters,
    ValidatedLeadRow,
    WorkflowSchema,
)
from solution.normalisation import (
    mask_phone,
    normalise_header,
    normalise_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_string,
)
from solution.pii_protection import PIIProtector
from solution.schema_loader import SchemaLoader

logger = logging.getLogger(__name__)


class LeadIngestionService:
    """Processes lead CSV files according to workflow schemas."""

    def __init__(
        self,
        db: Database,
        schema_loader: SchemaLoader,
        pii_protector: PIIProtector,
        linking_service: Any = None,
    ) -> None:
        self.db = db
        self.schema_loader = schema_loader
        self.pii_protector = pii_protector
        self.linking_service = linking_service

    def ingest_lead_file(
        self,
        drop_name: str,
        workflow_id: str,
        file_path: Path | str,
        counters: DataQualityCounters,
    ) -> None:
        """Ingest a single CSV lead batch file."""
        path = Path(file_path)
        batch_id = path.stem  # e.g., 'batch_001'
        schema = self.schema_loader.get_schema(workflow_id)

        # Check if already ingested for this drop
        with open(path, "rb") as f:
            content = f.read()
            file_hash = hashlib.sha256(content).hexdigest()

        with self.db.get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM batch_ingestion_log
                WHERE drop_name = ? AND workflow_id = ? AND batch_id = ?
                """,
                (drop_name, workflow_id, batch_id),
            ).fetchone()
            if existing:
                logger.info(
                    "Batch %s for workflow %s in %s already ingested, skipping.",
                    batch_id,
                    workflow_id,
                    drop_name,
                )
                return

        logger.info(
            "Ingesting leads from %s (workflow=%s, batch=%s)",
            path.name,
            workflow_id,
            batch_id,
        )

        text_content = content.decode("utf-8-sig", errors="replace")
        lines = text_content.splitlines()
        if not lines:
            return

        reader = csv.reader(lines)
        try:
            raw_header = next(reader)
        except StopIteration:
            return

        # Map headers
        header_mapping = self._map_headers(raw_header, schema)

        row_number = 0
        file_non_blank = 0
        file_rejected = 0
        file_merged = 0
        file_warnings = 0

        # Process rows
        with self.db.transaction() as conn:
            for row_cells in reader:
                # Check for blank row (every cell empty or whitespace)
                if not row_cells or all(not cell.strip() for cell in row_cells):
                    continue

                row_number += 1
                file_non_blank += 1
                counters.lead_rows_total += 1

                # Construct raw row dict
                raw_row: dict[str, str] = {}
                for idx, cell_val in enumerate(row_cells):
                    if idx in header_mapping:
                        field_name = header_mapping[idx]
                        raw_row[field_name] = cell_val

                # Validate and parse row
                validated = self._validate_row(
                    raw_row, schema, workflow_id, batch_id, row_number
                )

                if not validated.is_valid:
                    file_rejected += 1
                    counters.lead_rows_rejected += 1
                    reason = validated.rejection_reason or "unknown_rejection"
                    counters.rejections_by_reason[reason] = (
                        counters.rejections_by_reason.get(reason, 0) + 1
                    )

                    # Insert into rejected_leads
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO rejected_leads
                            (drop_name, workflow_id, batch_id, row_number, reason, created_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                        """,
                        (drop_name, workflow_id, batch_id, row_number, reason),
                    )
                else:
                    # Valid row: accumulate warnings
                    file_warnings += validated.warnings
                    counters.field_warnings += validated.warnings

                    # Insert or merge lead
                    merged = self._persist_lead(conn, validated, schema)
                    if merged:
                        file_merged += 1
                        counters.duplicate_lead_rows_merged += 1

            # Record batch ingestion log
            conn.execute(
                """
                INSERT OR REPLACE INTO batch_ingestion_log
                    (drop_name, workflow_id, batch_id, file_path, file_hash,
                     non_blank_rows, rejected_rows, merged_duplicates, field_warnings, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    drop_name,
                    workflow_id,
                    batch_id,
                    str(path),
                    file_hash,
                    file_non_blank,
                    file_rejected,
                    file_merged,
                    file_warnings,
                ),
            )

    def _map_headers(
        self, raw_header: list[str], schema: WorkflowSchema
    ) -> dict[int, str]:
        """
        Map CSV column indices to schema field names:
        - Header normalisation: lowercase, trim, runs of whitespace or underscore -> single space.
        - Match against field name or aliases (normalised the same way).
        - Unmatched columns are ignored.
        """
        lookup: dict[str, str] = {}
        for f in schema.fields:
            lookup[normalise_header(f.name)] = f.name
            for alias in f.aliases:
                lookup[normalise_header(alias)] = f.name

        mapping: dict[int, str] = {}
        for idx, col in enumerate(raw_header):
            norm = normalise_header(col)
            if norm in lookup:
                mapping[idx] = lookup[norm]
        return mapping

    def _validate_row(
        self,
        raw_row: dict[str, str],
        schema: WorkflowSchema,
        workflow_id: str,
        batch_id: str,
        row_number: int,
    ) -> ValidatedLeadRow:
        """
        Validate fields in schema order:
        - Required field missing -> missing_required:<field_name>
        - Phone present but invalid -> invalid_phone
        - Optional invalid fields -> store None and accumulate field warning
        """
        row_warnings = 0
        parsed_fields: dict[str, Any] = {}
        canonical_phone: str | None = None
        external_ref: str | None = None

        for field_def in schema.fields:
            raw_val = raw_row.get(field_def.name)
            is_empty = raw_val is None or not raw_val.strip()

            if is_empty:
                if field_def.required:
                    return ValidatedLeadRow(
                        workflow_id=workflow_id,
                        batch_id=batch_id,
                        row_number=row_number,
                        is_valid=False,
                        rejection_reason=f"missing_required:{field_def.name}",
                    )
                parsed_fields[field_def.name] = None
                continue

            # Value is present, parse based on type
            assert raw_val is not None
            val_str = raw_val.strip()

            if field_def.type == "phone":
                p_canon, p_valid = normalise_phone(val_str)
                if not p_valid:
                    return ValidatedLeadRow(
                        workflow_id=workflow_id,
                        batch_id=batch_id,
                        row_number=row_number,
                        is_valid=False,
                        rejection_reason="invalid_phone",
                    )
                canonical_phone = p_canon
                parsed_fields[field_def.name] = p_canon

            elif field_def.type == "string":
                s_val, _ = parse_string(val_str)
                parsed_fields[field_def.name] = s_val
                if field_def.reference:
                    external_ref = s_val

            elif field_def.type == "date":
                d_val, d_valid = parse_date(val_str)
                if not d_valid:
                    if field_def.required:
                        # Should not happen with current schemas, but defend
                        return ValidatedLeadRow(
                            workflow_id=workflow_id,
                            batch_id=batch_id,
                            row_number=row_number,
                            is_valid=False,
                            rejection_reason=f"missing_required:{field_def.name}",
                        )
                    parsed_fields[field_def.name] = None
                    row_warnings += 1
                else:
                    parsed_fields[field_def.name] = d_val

            elif field_def.type == "decimal":
                dec_val, dec_valid = parse_decimal(val_str)
                if not dec_valid:
                    if field_def.required:
                        return ValidatedLeadRow(
                            workflow_id=workflow_id,
                            batch_id=batch_id,
                            row_number=row_number,
                            is_valid=False,
                            rejection_reason=f"missing_required:{field_def.name}",
                        )
                    parsed_fields[field_def.name] = None
                    row_warnings += 1
                else:
                    parsed_fields[field_def.name] = dec_val

            elif field_def.type == "enum":
                enum_val, enum_valid = parse_enum(val_str, field_def.values)
                if not enum_valid:
                    if field_def.required:
                        return ValidatedLeadRow(
                            workflow_id=workflow_id,
                            batch_id=batch_id,
                            row_number=row_number,
                            is_valid=False,
                            rejection_reason=f"missing_required:{field_def.name}",
                        )
                    parsed_fields[field_def.name] = None
                    row_warnings += 1
                else:
                    parsed_fields[field_def.name] = enum_val

        # Ensure external reference is populated
        if external_ref is None and schema.reference_field.name in parsed_fields:
            external_ref = str(parsed_fields[schema.reference_field.name] or "")

        return ValidatedLeadRow(
            workflow_id=workflow_id,
            batch_id=batch_id,
            row_number=row_number,
            is_valid=True,
            warnings=row_warnings,
            phone_canonical=canonical_phone,
            external_ref=external_ref,
            field_values=parsed_fields,
        )

    def _persist_lead(
        self,
        conn: Any,
        validated: ValidatedLeadRow,
        schema: WorkflowSchema,
    ) -> bool:
        """
        Insert or merge lead in database:
        - If lead exists: overwrite non-null fields; retain original batch_id; return True (merged).
        - If new lead: insert lead; return False.
        - PII fields encrypted.
        """
        canonical_phone = validated.phone_canonical
        assert canonical_phone is not None

        phone_fingerprint = self.pii_protector.compute_phone_fingerprint(
            canonical_phone
        )
        encrypted_phone = self.pii_protector.encrypt(canonical_phone)
        phone_masked = mask_phone(canonical_phone)
        external_ref = validated.external_ref or ""

        existing = conn.execute(
            """
            SELECT id, batch_id, field_values_json
            FROM leads
            WHERE workflow_id = ? AND phone_fingerprint = ?
            """,
            (validated.workflow_id, phone_fingerprint),
        ).fetchone()

        now_iso = datetime.now(UTC).isoformat()

        if existing:
            # Duplicate lead: merge non-null fields
            existing_values = json.loads(existing["field_values_json"])
            for k, v in validated.field_values.items():
                if v is not None:
                    # If field is PII, encrypt it
                    f_def = schema.fields_by_name.get(k)
                    if f_def and f_def.pii:
                        existing_values[k] = self.pii_protector.encrypt(str(v))
                    else:
                        existing_values[k] = v

            # Update external_ref if the reference field has a new non-null value
            ref_field_name = schema.reference_field.name
            new_ref = validated.field_values.get(ref_field_name)
            updated_ref = (
                str(new_ref)
                if new_ref is not None
                else existing.get("external_ref", external_ref)
            )

            conn.execute(
                """
                UPDATE leads
                SET field_values_json = ?, external_ref = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(existing_values), updated_ref, now_iso, existing["id"]),
            )
            if self.linking_service:
                self.linking_service.register_lead(
                    validated.workflow_id, phone_fingerprint, existing["id"]
                )
            return True
        else:
            # First write: prepare field values with encrypted PII
            stored_values: dict[str, Any] = {}
            for k, v in validated.field_values.items():
                if v is not None:
                    f_def = schema.fields_by_name.get(k)
                    if f_def and f_def.pii:
                        stored_values[k] = self.pii_protector.encrypt(str(v))
                    else:
                        stored_values[k] = v
                else:
                    stored_values[k] = None

            cur = conn.execute(
                """
                INSERT INTO leads
                    (workflow_id, phone_fingerprint, encrypted_phone, phone_masked,
                     external_ref, batch_id, field_values_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.workflow_id,
                    phone_fingerprint,
                    encrypted_phone,
                    phone_masked,
                    external_ref,
                    validated.batch_id,
                    json.dumps(stored_values),
                    now_iso,
                    now_iso,
                ),
            )
            if self.linking_service:
                self.linking_service.register_lead(
                    validated.workflow_id, phone_fingerprint, cur.lastrowid
                )
            return False
