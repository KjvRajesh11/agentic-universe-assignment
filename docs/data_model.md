# Relational Data Model & Schema Design

## Database Schema Overview

The database is built on SQLite with WAL (`Write-Ahead Logging`) mode enabled, foreign key enforcement, and explicit transactional boundaries.

```mermaid
erDiagram
    LEADS ||--o{ INTERACTION_LEDGER : "has interaction events"
    LEADS {
        int id PK
        string workflow_id
        string phone_fingerprint UK
        string encrypted_phone
        string phone_masked
        string external_ref
        string batch_id
        string field_values_json
        timestamp created_at
        timestamp updated_at
    }

    INTERACTION_LEDGER {
        int id PK
        string event_id UK
        string event_type
        string workflow_id
        string call_id
        string occurred_at
        float occurred_at_epoch
        string ist_date
        int lead_id FK
        string phone_fingerprint
        string payload_json
        string drop_name
        timestamp created_at
    }

    QUARANTINED_EVENTS {
        int id PK
        string event_id UK
        string event_type
        string workflow_id
        string call_id
        string occurred_at
        float occurred_at_epoch
        string ist_date
        string phone_canonical
        string phone_fingerprint
        string payload_json
        string drop_name
        string reason
        timestamp created_at
    }

    REJECTED_LEADS {
        int id PK
        string drop_name
        string workflow_id
        string batch_id
        int row_number
        string reason
        timestamp created_at
    }

    INGESTION_CHECKPOINTS {
        int id PK
        string drop_name UK
        string drop_path
        string status
        timestamp started_at
        timestamp completed_at
    }

    BATCH_INGESTION_LOG {
        int id PK
        string drop_name
        string workflow_id
        string batch_id
        string file_path
        string file_hash
        int non_blank_rows
        int rejected_rows
        int merged_duplicates
        int field_warnings
        timestamp ingested_at
    }

    EVENT_FILE_INGESTION_LOG {
        int id PK
        string drop_name
        string file_path
        string file_hash
        int total_lines
        int malformed_lines
        int duplicate_lines
        int unknown_disposition_lines
        timestamp ingested_at
    }
```

## Entity Descriptions

### 1. `leads`
- **Primary Key:** `id` (INTEGER AUTOINCREMENT)
- **Natural Business Key:** `(workflow_id, phone_fingerprint)`
- **Attributes:**
  - `phone_fingerprint`: 64-character HMAC-SHA256 hex string of canonical phone (`+91XXXXXXXXXX`).
  - `encrypted_phone`: Base64 AES-256-GCM ciphertext with authenticated 12-byte nonce.
  - `phone_masked`: Safe masked representation (`+91******XXXX`).
  - `external_ref`: Business reference key (e.g. `policy_number`, `order_id`).
  - `batch_id`: Permanent file stem of the first valid row that introduced this lead.
  - `field_values_json`: JSON-serialized dictionary of normalized attributes (with sensitive fields encrypted).
- **Constraints & Indexes:**
  - `UNIQUE (workflow_id, phone_fingerprint)`
  - Index on `(workflow_id, external_ref)`
  - Index on `(workflow_id, batch_id)`

### 2. `interaction_ledger`
- **Primary Key:** `id`
- **Natural Key:** `event_id` (UNIQUE)
- **Attributes:**
  - `event_type`: e.g. `call.initiated`, `call.connected`, `call.telephony_failed`, `call.ended`, `disposition.set`, `disposition.override`.
  - `workflow_id`: Workflow identifier.
  - `call_id`: Call instance identifier.
  - `occurred_at`: ISO-8601 formatted timestamp in IST (`YYYY-MM-DDTHH:MM:SS+05:30`).
  - `occurred_at_epoch`: Floating-point epoch seconds for deterministic sorting.
  - `ist_date`: IST date string (`YYYY-MM-DD`) for daily overview partitioning.
  - `lead_id`: Foreign key referencing `leads(id)`.
  - `payload_json`: Raw JSON event payload.
- **Constraints & Indexes:**
  - `event_id UNIQUE`
  - Index on `call_id`
  - Index on `lead_id`
  - Index on `(workflow_id, ist_date)`
  - Index on `(call_id, occurred_at_epoch)`

### 3. `quarantined_events`
- Stores valid interaction events that arrived prior to the corresponding lead being uploaded.
- Retains full metadata and event payload for deterministic replay when later drops arrive.

### 4. `rejected_leads`
- Stores discarded CSV rows that failed schema validation.
- Contains `drop_name`, `workflow_id`, `batch_id`, `row_number`, and `reason` (e.g., `missing_required:customer_name`, `invalid_phone`).
- **Strictly contains zero PII.**

### 5. Ingestion Logs & Checkpoints
- `ingestion_checkpoints`: Tracks drop-level completion to ensure idempotency.
- `batch_ingestion_log`: Records per-file CSV metrics and SHA-256 content hashes.
- `event_file_ingestion_log`: Records per-file JSONL metrics and SHA-256 content hashes.
