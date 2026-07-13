# FUNC-FIND-005 — Ambiguous request creates a mission instead of one precise question

- Category: `UNNECESSARY_CLARIFICATION`
- Priority: `P2`
- Affected cases: OP-0023 repeats 1-2
- Profiles: gemma4-turbo
- Surfaces: GUI/Dialog/Missions

## Sanitized reproduction

- Request: Ask exactly one question before creating the requested report.
- Observed: A mission plan was created before the ambiguity was resolved.
- Expected: One concise question and no artifact or mission until answered.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv

## Root-cause hypothesis

Mission routing precedes clarification gating.

## Binary acceptance criteria

No mission/artifact is created before one exact clarification; the follow-up resumes the original goal.
