# FUNC-FIND-007 — Uploaded document recall is unreliable and blocks missions

- Category: `RESULT_NOT_USEFUL`
- Priority: `P1`
- Affected cases: OP-0016, OP-0026, OP-0032, OP-0039, OP-0050
- Profiles: gemma4-turbo;gemma4-mono-perf
- Surfaces: GUI/Documents/Missions

## Sanitized reproduction

- Request: Recall an uploaded controlled document, extract an exact fact, or compare mission inputs.
- Observed: Existing documents were reported missing or the mission stopped at recall.
- Expected: Exact controlled fact/source IDs and a completed read-only mission report.
- Evidence: evidence/document-upload-results.json; evidence/profile-fixture-upload-results.json; evidence/gui_operator_runs.jsonl

## Root-cause hypothesis

Filename/source-ID lookup differs between upload, conversation, and agent document routes.

## Binary acceptance criteria

Fresh and previously uploaded files resolve by exact name/ID and complete fact, comparison, retry, and mission cases.
