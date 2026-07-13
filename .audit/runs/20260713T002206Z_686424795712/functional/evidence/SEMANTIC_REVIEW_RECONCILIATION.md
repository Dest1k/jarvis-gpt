# Semantic Review Reconciliation

- Sources: `SEMANTIC_REVIEW_PASS_1.csv` and `SEMANTIC_REVIEW_PASS_2.csv` only.
- Join key: source `(operator_case_id, repeat)`, emitted as `(case_id, repeat)`.
- Coverage: **107/107** keys from each pass; both key sets are identical and contain no duplicates or blanks.
- Rule: preserve both source statuses; use the shared status when equal, otherwise use `INCONCLUSIVE`.

## Result

- Equal statuses: 95 rows — 46 `PASS`, 47 `FAIL`, 2 `INCONCLUSIVE`.
- Different statuses: 12 rows; all reconcile to `INCONCLUSIVE`.
- Accepted totals: 46 `PASS`, 47 `FAIL`, 14 `INCONCLUSIVE`.

| Key | Pass 1 | Pass 2 | Accepted |
|---|---|---|---|
| OP-0019 / 1 | PASS | FAIL | INCONCLUSIVE |
| OP-0019 / 2 | PASS | FAIL | INCONCLUSIVE |
| OP-0033 / 1 | BLOCKED_BY_ENV | FAIL | INCONCLUSIVE |
| OP-0033 / 2 | BLOCKED_BY_ENV | FAIL | INCONCLUSIVE |
| OP-0035 / 3 | FAIL | INCONCLUSIVE | INCONCLUSIVE |
| OP-0037 / 2 | BLOCKED_BY_ENV | FAIL | INCONCLUSIVE |
| OP-0037 / 3 | BLOCKED_BY_ENV | FAIL | INCONCLUSIVE |
| OP-0040 / 2 | INCONCLUSIVE | PASS | INCONCLUSIVE |
| OP-0040 / 3 | INCONCLUSIVE | PASS | INCONCLUSIVE |
| OP-0043 / 1 | FAIL | BLOCKED_BY_ENV | INCONCLUSIVE |
| OP-0043 / 2 | FAIL | BLOCKED_BY_ENV | INCONCLUSIVE |
| OP-0043 / 3 | FAIL | BLOCKED_BY_ENV | INCONCLUSIVE |

Source review files were not modified.
