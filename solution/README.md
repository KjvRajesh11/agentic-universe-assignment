# Assignment A: Interaction Ledger & Reporting Pipeline — Solution

Engineered by **KJV Rajesh** for the Agentic Universe Junior SDE Assessment.

---

## Quickstart & Execution

### 1. Environment Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure security environment keys (or copy .env.example)
cp .env.example .env
```

### 2. Run Test Suite

```bash
pytest -v
```

### 3. Generate Synthetic Data & Run Pipeline

```bash
# 1. Generate small dataset (or use any seed)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 2. Ingest Drop 1
python -m solution.main ingest data/drop_1

# 3. Ingest Drop 2
python -m solution.main ingest data/drop_2

# 4. Generate All 5 Client Reports
python -m solution.main report --out reports/
```

Generated reports will be available in `reports/`:
- `reports/batch_summary.csv`
- `reports/daily_overview.csv`
- `reports/lead_report.csv`
- `reports/rejections.csv`
- `reports/data_quality.json`

---

## Architectural Highlights

- **Dynamic Schema Ingestion:** Schema-driven dynamic parser loads `motor_renewal.json` and `policy_feedback.json` without hardcoded column dependencies. Maps messy headers, ignores unmapped columns, enforces day-first date validation, cleans decimal currency prefixes, and handles casing.
- **Durable Append-Only Ledger:** Events are written strictly to an append-only ledger. Overrides create new entries; effective outcomes are dynamically resolved without mutating history.
- **Cryptographic PII Protection:**
  - AES-256-GCM authenticated encryption for customer names and telephone numbers.
  - Keyed HMAC-SHA256 deterministic phone fingerprinting for identity matching.
  - Zero plaintext PII in database, logs, or rejections.
- **Orphan Reconciliation Loop:** Out-of-order interaction events arriving before lead uploads are safely quarantined and automatically relinked upon subsequent lead drops.
- **Idempotency & Checkpointing:** Checkpoints prevent duplicate re-ingestion, ensuring byte-identical report outputs across repeated executions.
- **High Performance:** Ingests and reports the 140,000-lead / 632,000-event large dataset in **64.76 seconds** (well under the 2-minute benchmark stretch goal).

---

## Verification & Benchmarks

| Metric | Measured Result |
| :--- | :--- |
| **Test Suite** | 39 passed in 0.66s |
| **Linting & Formatting** | Ruff checks: 100% clean |
| **Large Scale Total Time** | 64.76s (Target: < 120s) |
| **Large Scale DB Size** | 333.48 MB |
