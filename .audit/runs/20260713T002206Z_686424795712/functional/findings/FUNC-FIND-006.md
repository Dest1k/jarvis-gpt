# FUNC-FIND-006 — Raw tool-call envelopes reach rendered assistant output

- Category: `INTERNAL_OUTPUT_LEAK`
- Priority: `P1`
- Affected cases: OP-0025, OP-0028..OP-0030, OP-0034, OP-0036, OP-0044
- Profiles: gemma4-turbo
- Surfaces: GUI/API stream

## Sanitized reproduction

- Request: Perform document/runtime tasks and return a normal user answer.
- Observed: Raw call:documents, call:llm.health, call:dispatcher.status, or JSON tool payloads were rendered.
- Expected: Only validated tool results and one natural final answer are visible.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv

## Root-cause hypothesis

Tool-shaped output bypasses the final response integrity classifier on some routes.

## Binary acceptance criteria

Known marker scan finds zero tool envelopes in DOM, NDJSON deltas, and terminal answers over all affected cases.
