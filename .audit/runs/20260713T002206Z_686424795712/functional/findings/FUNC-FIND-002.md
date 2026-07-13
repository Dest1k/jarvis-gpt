# FUNC-FIND-002 — Exact response constraints are not consistently enforced

- Category: `FORMAT_BREACH`
- Priority: `P2`
- Affected cases: OP-0007, OP-0010, OP-0024
- Profiles: gemma4-turbo
- Surfaces: GUI/Dialog

## Sanitized reproduction

- Request: Return exact count/JSON/default-assumption formats.
- Observed: Rendered output violated requested count, schema, or assumption contract.
- Expected: Deterministically valid output matching the explicit constraint.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_PASS_1.csv; evidence/SEMANTIC_REVIEW_PASS_2.csv

## Root-cause hypothesis

Final answer validation does not cover ordinary non-tool format contracts.

## Binary acceptance criteria

All listed cases pass their deterministic parser/count validators in three consecutive runs.
