# Profile Semantic Review Reconciliation

- Sources: `PROFILE_SEMANTIC_REVIEW_PASS_1.csv` and `PROFILE_SEMANTIC_REVIEW_PASS_2.csv` only.
- Scope: exactly 62 unique `(operator_case_id, repeat)` keys within `OP-0045` through `OP-0068`.
- Coverage: **62/62** rows in each pass; key sets are identical, with no duplicate, blank, out-of-range, or missing keys.
- Rule applied literally: preserve both source statuses; any disagreement becomes `INCONCLUSIVE`.

## Result

- Agreements: 2 — `OP-0050/1` and `OP-0050/2`, both `FAIL`.
- Differences: 60 — 55 `BLOCKED_BY_ENV` versus `FAIL`, and 5 `BLOCKED_BY_SPEC` versus `BLOCKED_BY_ENV`.
- Accepted totals: 2 `FAIL`, 60 `INCONCLUSIVE`.

| Pass 1 | Pass 2 | Rows | Accepted |
|---|---|---:|---|
| FAIL | FAIL | 2 | FAIL |
| BLOCKED_BY_ENV | FAIL | 55 | INCONCLUSIVE |
| BLOCKED_BY_SPEC | BLOCKED_BY_ENV | 5 | INCONCLUSIVE |

Source review files were not modified.
