"""Data structures and domain models for the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldDefinition:
    """Schema definition for a single column."""

    name: str
    type: str  # 'string', 'phone', 'date', 'decimal', 'enum'
    required: bool = False
    pii: bool = False
    reference: bool = False
    default: str | None = None
    values: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowSchema:
    """Complete schema definition for a workflow."""

    workflow_id: str
    description: str
    fields: list[FieldDefinition]

    @property
    def reference_field(self) -> FieldDefinition:
        """Return the schema field marked as reference."""
        for f in self.fields:
            if f.reference:
                return f
        raise ValueError(f"No reference field defined in schema for {self.workflow_id}")

    @property
    def fields_by_name(self) -> dict[str, FieldDefinition]:
        """Index fields by their canonical name."""
        return {f.name: f for f in self.fields}


@dataclass(frozen=True)
class Disposition:
    """Entry in the disposition catalogue."""

    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


@dataclass
class ValidatedLeadRow:
    """A single parsed row from a lead CSV."""

    workflow_id: str
    batch_id: str
    row_number: int
    is_valid: bool
    rejection_reason: str | None = None
    warnings: int = 0
    phone_canonical: str | None = None
    external_ref: str | None = None
    field_values: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventRecord:
    """Parsed interaction event."""

    event_id: str
    event_type: str
    workflow_id: str
    call_id: str
    occurred_at: str
    occurred_at_epoch: float
    ist_date: str
    phone: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DataQualityCounters:
    """In-memory or persistent counters for data quality reporting."""

    lead_rows_total: int = 0
    lead_rows_rejected: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)
    duplicate_lead_rows_merged: int = 0
    field_warnings: int = 0
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0
    orphan_events: int = 0
    unknown_disposition_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Produce the exact dictionary structure required by data_quality.json."""
        return {
            "lead_rows_total": self.lead_rows_total,
            "lead_rows_rejected": self.lead_rows_rejected,
            "rejections_by_reason": dict(sorted(self.rejections_by_reason.items())),
            "duplicate_lead_rows_merged": self.duplicate_lead_rows_merged,
            "field_warnings": self.field_warnings,
            "events_total": self.events_total,
            "malformed_events": self.malformed_events,
            "duplicate_events_dropped": self.duplicate_events_dropped,
            "orphan_events": self.orphan_events,
            "unknown_disposition_events": self.unknown_disposition_events,
        }
