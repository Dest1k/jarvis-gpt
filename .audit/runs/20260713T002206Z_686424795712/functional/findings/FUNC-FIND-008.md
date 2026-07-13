# FUNC-FIND-008 — Corrupt document recovery is inconsistent

- Category: `ERROR_NOT_ACTIONABLE`
- Priority: `P2`
- Affected cases: OP-0033 repeat 3
- Profiles: gemma4-turbo
- Surfaces: GUI/Documents

## Sanitized reproduction

- Request: Open a corrupt file, report the failure, then retry with a valid replacement.
- Observed: At least one repeat did not provide a clean actionable error/retry result.
- Expected: No false success or stale partial output; valid replacement succeeds.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/DOCUMENT_FIXTURE_QA.md

## Root-cause hypothesis

Parser failure and retry state are not consistently normalized.

## Binary acceptance criteria

Three corrupt-to-valid retries show one actionable error followed by one clean result with no stale content.
