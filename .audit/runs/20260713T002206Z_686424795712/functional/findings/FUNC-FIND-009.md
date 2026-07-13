# FUNC-FIND-009 — Approved safe action uses a non-canonical tool schema

- Category: `TOOL_STATE_MISMATCH`
- Priority: `P1`
- Affected cases: OP-0037 repeats 1-3
- Profiles: gemma4-turbo
- Surfaces: GUI/Approvals/Tools

## Sanitized reproduction

- Request: Create one controlled directory through the exact approval flow.
- Observed: Approval execution referenced filesystem.mkdir while the allowed action is fs.mkdir; no directory was created.
- Expected: One pending approval, exact operator approval, one execution, and verified state change.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_PASS_1.csv; evidence/SEMANTIC_REVIEW_PASS_2.csv

## Root-cause hypothesis

Model-facing aliases are not canonicalized before approval schema validation.

## Binary acceptance criteria

Three end-to-end approvals bind the canonical action and create only the approved path exactly once.
