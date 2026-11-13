"""Disposition service resolving effective dispositions and catalogue lookups."""

from __future__ import annotations

from typing import Any

from solution.models import Disposition
from solution.schema_loader import SchemaLoader


class DispositionService:
    """Provides disposition validation and effective disposition resolution."""

    def __init__(self, schema_loader: SchemaLoader) -> None:
        self.schema_loader = schema_loader

    def is_valid_code(self, workflow_id: str, code: str) -> bool:
        """Check if disposition code is valid for workflow."""
        return self.schema_loader.is_valid_disposition(workflow_id, code)

    def get_disposition(self, workflow_id: str, code: str) -> Disposition | None:
        """Get disposition metadata (is_terminal, is_success)."""
        return self.schema_loader.get_disposition(workflow_id, code)

    def resolve_effective_disposition(
        self,
        workflow_id: str,
        events: list[dict[str, Any]],
    ) -> tuple[str | None, Disposition | None]:
        """
        Compute effective disposition for a set of events belonging to a single call:
        1. Latest valid disposition.override by occurred_at_epoch
        2. Otherwise latest valid disposition.set by occurred_at_epoch
        3. Otherwise (None, None)
        """
        overrides: list[tuple[float, str]] = []
        sets: list[tuple[float, str]] = []

        for e in events:
            etype = e.get("event_type")
            if etype not in ("disposition.set", "disposition.override"):
                continue

            payload = e.get("payload") or {}
            code = payload.get("disposition_code")
            if not code or not self.is_valid_code(workflow_id, code):
                continue

            epoch = float(e.get("occurred_at_epoch", 0.0))
            if etype == "disposition.override":
                overrides.append((epoch, code))
            elif etype == "disposition.set":
                sets.append((epoch, code))

        if overrides:
            overrides.sort(key=lambda x: x[0], reverse=True)
            chosen_code = overrides[0][1]
            return chosen_code, self.get_disposition(workflow_id, chosen_code)

        if sets:
            sets.sort(key=lambda x: x[0], reverse=True)
            chosen_code = sets[0][1]
            return chosen_code, self.get_disposition(workflow_id, chosen_code)

        return None, None
