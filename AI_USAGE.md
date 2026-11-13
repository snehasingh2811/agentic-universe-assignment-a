# AI Usage Disclosure

## Tools used
OpenAI Codex.

## What I used it for
I used Codex as a coding assistant to interpret the Assignment A requirements, draft the SQLite ingestion and reporting pipeline, add initial tests, and prepare the run and design documentation. I then ran the setup, test, and sample-data commands locally and checked that the five report files were produced.

## What I wrote or decided myself
I chose Assignment A and reviewed the proposed design against the task requirements. I am responsible for understanding the submitted implementation, including its schema, PII handling, idempotency behavior, event linking, and report calculations. Before the interview or submission, I will read through the code and make sure I can explain and modify it myself.

## Where AI was wrong or needed correction
An initial report implementation failed because its batch aggregation list was not initialized. The sample run exposed the error, and I used Codex to diagnose and fix it before generating the reports again. That reinforced the need to run generated code and inspect its output.

## How I verified the work
I ran `python -m pytest -q`; all 3 tests passed. I generated the supplied small dataset, ingested both drops, ran the report command, and confirmed that all five report files appeared in `reports/`. The sample run is a smoke test; the evaluator's fresh-seed run remains the stronger correctness check.
