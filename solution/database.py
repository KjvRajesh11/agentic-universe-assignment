"""SQLite database abstraction with connection management and transactional boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class Database:
    """Manages SQLite connection, schema migration, and transaction management."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def get_connection(self) -> sqlite3.Connection:
        """Create and configure a new SQLite connection."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager providing an atomic database transaction."""
        conn = self.get_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """Create all required tables, constraints, and indexes."""
        with self.get_connection() as conn:
            conn.executescript("""
            -- Leads Table: Identifies leads by (workflow_id, phone_fingerprint)
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                phone_fingerprint TEXT NOT NULL,
                encrypted_phone TEXT NOT NULL,
                phone_masked TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                field_values_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (workflow_id, phone_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_leads_wf_fp ON leads (workflow_id, phone_fingerprint);
            CREATE INDEX IF NOT EXISTS idx_leads_wf_ref ON leads (workflow_id, external_ref);
            CREATE INDEX IF NOT EXISTS idx_leads_wf_batch ON leads (workflow_id, batch_id);

            -- Rejected Lead Rows
            CREATE TABLE IF NOT EXISTS rejected_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drop_name TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (drop_name, workflow_id, batch_id, row_number)
            );
            CREATE INDEX IF NOT EXISTS idx_rej_wf_batch_row ON rejected_leads (workflow_id, batch_id, row_number);

            -- Append-only Interaction Ledger
            CREATE TABLE IF NOT EXISTS interaction_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                occurred_at_epoch REAL NOT NULL,
                ist_date TEXT NOT NULL,
                lead_id INTEGER NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
                phone_fingerprint TEXT,
                payload_json TEXT NOT NULL,
                drop_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_event_id ON interaction_ledger (event_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_call_id ON interaction_ledger (call_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_lead_id ON interaction_ledger (lead_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_wf_ist_date ON interaction_ledger (workflow_id, ist_date);
            CREATE INDEX IF NOT EXISTS idx_ledger_call_time ON interaction_ledger (call_id, occurred_at_epoch);

            -- Quarantined / Unlinked Events (Orphans)
            CREATE TABLE IF NOT EXISTS quarantined_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                occurred_at_epoch REAL NOT NULL,
                ist_date TEXT NOT NULL,
                phone_canonical TEXT,
                phone_fingerprint TEXT,
                payload_json TEXT NOT NULL,
                drop_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_quar_event_id ON quarantined_events (event_id);
            CREATE INDEX IF NOT EXISTS idx_quar_wf_fp ON quarantined_events (workflow_id, phone_fingerprint);
            CREATE INDEX IF NOT EXISTS idx_quar_call_id ON quarantined_events (call_id);

            -- Checkpoint for drops to enforce idempotency
            CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drop_name TEXT NOT NULL UNIQUE,
                drop_path TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );

            -- Metrics log per batch file
            CREATE TABLE IF NOT EXISTS batch_ingestion_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drop_name TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                non_blank_rows INTEGER NOT NULL,
                rejected_rows INTEGER NOT NULL,
                merged_duplicates INTEGER NOT NULL,
                field_warnings INTEGER NOT NULL,
                ingested_at TEXT NOT NULL,
                UNIQUE (drop_name, workflow_id, batch_id)
            );

            -- Metrics log per event file
            CREATE TABLE IF NOT EXISTS event_file_ingestion_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drop_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                total_lines INTEGER NOT NULL,
                malformed_lines INTEGER NOT NULL,
                duplicate_lines INTEGER NOT NULL,
                unknown_disposition_lines INTEGER NOT NULL,
                ingested_at TEXT NOT NULL,
                UNIQUE (drop_name, file_path)
            );
            """)

    def is_drop_completed(self, drop_name: str) -> bool:
        """Check if a drop was already completed."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM ingestion_checkpoints WHERE drop_name = ?",
                (drop_name,),
            ).fetchone()
            return bool(row and row["status"] == "COMPLETED")

    def record_drop_start(
        self, drop_name: str, drop_path: str, started_at: str
    ) -> None:
        """Record the start of a drop ingestion."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ingestion_checkpoints (drop_name, drop_path, status, started_at)
                VALUES (?, ?, 'STARTED', ?)
                ON CONFLICT(drop_name) DO UPDATE SET
                    status = 'STARTED',
                    started_at = excluded.started_at;
                """,
                (drop_name, drop_path, started_at),
            )

    def record_drop_complete(self, drop_name: str, completed_at: str) -> None:
        """Record the completion of a drop ingestion."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE ingestion_checkpoints
                SET status = 'COMPLETED', completed_at = ?
                WHERE drop_name = ?;
                """,
                (completed_at, drop_name),
            )
