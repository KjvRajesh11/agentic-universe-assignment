"""Loader for workflow JSON schemas and disposition CSV catalogue."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from solution.exceptions import SchemaLoadError
from solution.models import Disposition, FieldDefinition, WorkflowSchema


class SchemaLoader:
    """Loads and caches workflow schemas and disposition catalogue."""

    def __init__(
        self, schema_dir: Path | str, dispositions_path: Path | str | None = None
    ) -> None:
        self.schema_dir = Path(schema_dir)
        self.dispositions_path = (
            Path(dispositions_path)
            if dispositions_path
            else self.schema_dir / "dispositions.csv"
        )
        self._schemas: dict[str, WorkflowSchema] = {}
        self._dispositions: dict[tuple[str, str], Disposition] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self.schema_dir.exists():
            raise SchemaLoadError(f"Schema directory does not exist: {self.schema_dir}")

        # Load all JSON schemas in directory
        for json_file in sorted(self.schema_dir.glob("*.json")):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                wf_id = data["workflow_id"]
                fields = [
                    FieldDefinition(
                        name=f["name"],
                        type=f["type"],
                        required=f.get("required", False),
                        pii=f.get("pii", False),
                        reference=f.get("reference", False),
                        default=f.get("default"),
                        values=f.get("values", []),
                        aliases=f.get("aliases", []),
                    )
                    for f in data.get("fields", [])
                ]
                self._schemas[wf_id] = WorkflowSchema(
                    workflow_id=wf_id,
                    description=data.get("description", ""),
                    fields=fields,
                )
            except Exception as e:
                raise SchemaLoadError(
                    f"Failed to parse schema file {json_file}: {e}"
                ) from e

        # Load dispositions catalogue
        if not self.dispositions_path.exists():
            raise SchemaLoadError(
                f"Dispositions file does not exist: {self.dispositions_path}"
            )

        try:
            with open(self.dispositions_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wf = row["workflow_id"].strip()
                    code = row["code"].strip()
                    label = row["label"].strip()
                    is_term = row["is_terminal"].strip().lower() == "true"
                    is_succ = row["is_success"].strip().lower() == "true"
                    disp = Disposition(
                        workflow_id=wf,
                        code=code,
                        label=label,
                        is_terminal=is_term,
                        is_success=is_succ,
                    )
                    self._dispositions[(wf, code)] = disp
        except Exception as e:
            raise SchemaLoadError(
                f"Failed to load dispositions from {self.dispositions_path}: {e}"
            ) from e

    def get_schema(self, workflow_id: str) -> WorkflowSchema:
        """Retrieve schema by workflow_id, raising SchemaLoadError if unknown."""
        if workflow_id not in self._schemas:
            raise SchemaLoadError(f"Unknown workflow schema: '{workflow_id}'")
        return self._schemas[workflow_id]

    def get_all_schemas(self) -> dict[str, WorkflowSchema]:
        """Return all loaded schemas."""
        return self._schemas

    def get_disposition(self, workflow_id: str, code: str) -> Disposition | None:
        """Lookup disposition by workflow and code."""
        return self._dispositions.get((workflow_id, code))

    def is_valid_disposition(self, workflow_id: str, code: str) -> bool:
        """Check whether a disposition code is valid for a workflow."""
        return (workflow_id, code) in self._dispositions
