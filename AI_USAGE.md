# AI Usage Disclosure

## Tools used
- Antigravity / Gemini 3.8 Flash (interactive assistant)

## What I used them for
- Scaffolding the initial directory structure and typing boilerplates (`solution/models.py`, `solution/exceptions.py`).
- Generating candidate regex patterns for Indian phone and currency sanitization.
- Drafting initial pytest fixtures for normalisation edge cases.
- Reviewing SQLite PRAGMA tuning options for high-throughput batch writes.

## What I wrote or decided myself
- **Core Architecture & Data Modeling:** Designed the relational schema in SQLite, transaction boundaries, append-only ledger semantics, and checkpointing for idempotency.
- **Cryptographic Security Strategy:** Made the decision to use keyed HMAC-SHA256 phone fingerprinting alongside AES-256-GCM. Unkeyed SHA-256 would leave 10-digit phone numbers vulnerable to rainbow table precomputation.
- **Multi-pass Orphan Reconciliation Strategy:** Designed the iterative convergence algorithm in `solution/event_linking.py` to resolve dependent `disposition.override` records when earlier call events are un-orphaned.
- **Business Logic Precedence:** Designed the exact precedence rules in `BucketService` and `DispositionService` (handling overrides via epoch sorting, tie-breaking between telephony failure and non-failure calls).
- **In-Memory Caching for Benchmark Optimization:** Architected the caching layer (`(workflow_id, phone_fp) -> lead_id` and `call_id -> (lead_id, phone_fp)`) that brought the 632k large-scale event processing time down from over 2 minutes to 64 seconds.

## Where the AI was wrong or unhelpful
- **Log Masking Regex:** An early AI-generated regex pattern for `PIIRedactionFilter` (`(\+?91[\s-]?)?([6-9]\d{9})`) assumed that 10-digit mobile numbers would have contiguous digits after the prefix. However, synthetic data generator outputs formatted numbers like `+91 98765 43210` with internal spaces. The filter failed to mask them, which was caught by `tests/security/test_pii.py`. I rejected the regex and rewrote it to allow optional whitespace or hyphens between digits (`(?:\+?91[\s-]?)?(?:[6-9](?:[\s-]?\d){9})`).
- **SQLite executescript Transaction Collision:** The initial transaction helper executed `conn.executescript()` inside an explicit `conn.execute("BEGIN IMMEDIATE;")` block. Because Python's `sqlite3.executescript()` implicitly commits open transactions, attempting to call `COMMIT` afterwards threw `sqlite3.OperationalError: cannot commit - no transaction is active`. I diagnosed this via test failure logs and refactored the connection context manager to manage transaction boundaries cleanly.

## How I verified AI-generated code
- **Automated Test Suite:** Built and ran 39 comprehensive unit, security, integration, and regression tests with `pytest`.
- **Fresh Seed Execution:** Validated the end-to-end pipeline on an unseen random seed (`--seed 9999`) to confirm absence of hardcoded assumptions or dataset overfitting.
- **Static Analysis & Linting:** Enforced strict linting and formatting via `ruff check` and `ruff format`.
- **Manual Line-by-Line Review:** Audited all cryptographic calls, SQL queries, and error handling paths to guarantee zero plaintext PII persistence and fail-closed security.
