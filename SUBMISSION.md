# Submission

## Candidate
- **Name:** Sneha Singh
- **Assignment chosen:** A (Interaction Ledger & Reporting Pipeline)
- **Time spent:** 4 hours

## How to run
From the repository root, with Python 3.11+:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports
python -m pytest -q
```

The first run creates `ledger.sqlite3` and a separate `ledger.sqlite3.key` beside it. Keep the key private and back it up with the database; losing it makes encrypted PII unreadable. For a fresh run, remove both local files. Generated data and database/key files are ignored by git.

## Design
SQLite stores leads, ingest-drop markers, quality counters, rejections, and an event ledger. Lead identity is a unique `(workflow_id, phone_hash)`. A keyed HMAC digest supports equality matching without storing the phone, while Fernet encryption protects schema fields marked PII. The key lives in a separate file with owner-only creation permissions. Non-PII schema values are stored as JSON in the lead row. Event payloads remain JSON, and event rows are never updated or deleted; duplicate IDs are rejected by the primary key.

An ingest runs in one SQLite transaction and records the resolved drop path, making repeat ingestion a no-op. Reports resolve event links against all leads currently in the database, so previously orphaned events become visible after a later lead upload. Calls are derived only from linked `call.initiated` events. Effective dispositions are chosen from the latest override when any exists, otherwise the latest set. Report files are generated in Python from the persisted ledger.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Create a local Fernet key file beside the database | Easy clean-clone setup while keeping PII encrypted in SQLite; file is created with restrictive permissions | Require an externally managed key via environment variable, which complicates the take-home run path |
| Treat an unparseable event timestamp as non-reportable while preserving its ID for deduplication | Avoid silently assigning a false call date and allow later duplicate delivery to be recognized | Reject the entire drop; the spec does not define timestamp failure handling |
| Recompute report links on each report run | Correctly relinks orphans when a lead arrives in a later drop | Maintain mutable link rows incrementally |
| Batch ownership stays with the first valid lead row | Follows the stated identity and duplicate rules | Move a lead to the newest batch |

## Failure modes considered
Blank and malformed lead values, duplicate leads, malformed/duplicate events, unknown dispositions, duplicate drop ingestion, late leads, invalid event phones, and event ordering are handled. SQLite transactions prevent a partially marked drop on ordinary processing errors. Not handled: concurrent writers, key rotation, schema migrations, ingesting the same logical drop from a different path, malformed CSV structure beyond the standard library parser, and a durable quarantine UI. The local key file is suitable for this exercise, not a production key-management system.

## Testing
Six direct assertions passed for phone normalization, day-first dates and impossible dates, and decimal currency parsing/rounding. The small generated dataset was ingested twice across its drops, reports were generated, and rerunning the second drop left quality totals unchanged. Python compilation passed. The pytest suite is included but could not be run in this environment because pytest is unavailable here; run `python -m pytest -q` in a normal Python environment.

## Results
Small seed 42 run: 2,057 lead rows read, 108 rejected, 57 duplicate lead rows merged, 122 field warnings, and 8,745 event lines read. Final counts included 8 malformed events, 237 duplicate events, 19 unknown dispositions, and 461 orphan events. These are a smoke-test run, not the evaluator's fresh seed.

## If I had another day
1. Add golden report fixtures and randomized/property tests for event ordering and merge behavior.
2. Add database-level encryption and late-orphan integration tests, including fresh-seed comparisons.
3. Add schema versioning, key rotation support, and production secret management.
4. Measure large-scale performance and optimize report aggregation if needed.
