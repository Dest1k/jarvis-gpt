# Semantic Review Pass 2 — gemma4-turbo

## Scope and method

- Reviewed `OP-0001` through `OP-0044`: 44 cases and 107 required `(case, repeat)` pairs.
- Read only the new operator catalog and GUI run log for semantic judgment. New fixture/upload and isolated-home files were consulted only for deterministic facts, formats, paths, file existence, and postconditions.
- Did not read pass 1 output or any old audit queue, finding index, or prior extended prompt.
- Ignored the out-of-scope `FUNC-TURBO-001` records.
- Removed the leading `ASSISTANT` role label and timing line before judgment. Other visible content, including `JSON` labels, raw tool calls, raw page text, and internal continuation notes, remained part of the response.
- Selected the latest harness-complete record for every pair. Here, harness-complete means `final: true` with no explicit run error; a terminal response can still be semantically incomplete and therefore score `FAIL`.

## Score scale

Each catalog dimension is scored independently in the CSV:

- `2` — fully met or directly observed.
- `1` — partial, minor weakness, or material uncertainty.
- `0` — materially failed.
- `NA` — not exercised or not observable from permitted evidence.

The accepted status is holistic. A `PASS` may contain a score of `1` when the weakness is non-material and the user contract is still met.

## Accepted results

| Status | Repeats |
|---|---:|
| PASS | 48 |
| FAIL | 53 |
| INCONCLUSIVE | 3 |
| BLOCKED_BY_ENV | 3 |
| BLOCKED_BY_SPEC | 0 |
| UNSUPPORTED | 0 |
| **Total** | **107** |

| Case | R1 | R2 | R3 |
|---|---|---|---|
| OP-0001 | PASS | PASS | — |
| OP-0002 | PASS | PASS | — |
| OP-0003 | PASS | PASS | — |
| OP-0004 | PASS | PASS | — |
| OP-0005 | PASS | PASS | PASS |
| OP-0006 | FAIL | FAIL | — |
| OP-0007 | FAIL | FAIL | — |
| OP-0008 | PASS | PASS | — |
| OP-0009 | PASS | PASS | PASS |
| OP-0010 | FAIL | FAIL | — |
| OP-0011 | PASS | PASS | — |
| OP-0012 | PASS | PASS | — |
| OP-0013 | FAIL | FAIL | FAIL |
| OP-0014 | FAIL | FAIL | — |
| OP-0015 | PASS | PASS | — |
| OP-0016 | FAIL | FAIL | — |
| OP-0017 | PASS | PASS | PASS |
| OP-0018 | PASS | PASS | — |
| OP-0019 | FAIL | FAIL | — |
| OP-0020 | PASS | PASS | PASS |
| OP-0021 | PASS | PASS | PASS |
| OP-0022 | PASS | PASS | — |
| OP-0023 | FAIL | FAIL | — |
| OP-0024 | PASS | FAIL | — |
| OP-0025 | FAIL | FAIL | FAIL |
| OP-0026 | FAIL | FAIL | — |
| OP-0027 | PASS | PASS | — |
| OP-0028 | FAIL | FAIL | — |
| OP-0029 | PASS | FAIL | FAIL |
| OP-0030 | PASS | FAIL | FAIL |
| OP-0031 | FAIL | FAIL | — |
| OP-0032 | FAIL | FAIL | — |
| OP-0033 | FAIL | FAIL | FAIL |
| OP-0034 | PASS | FAIL | FAIL |
| OP-0035 | PASS | PASS | INCONCLUSIVE |
| OP-0036 | FAIL | FAIL | — |
| OP-0037 | FAIL | FAIL | FAIL |
| OP-0038 | FAIL | FAIL | FAIL |
| OP-0039 | FAIL | FAIL | FAIL |
| OP-0040 | FAIL | PASS | PASS |
| OP-0041 | INCONCLUSIVE | INCONCLUSIVE | — |
| OP-0042 | PASS | PASS | PASS |
| OP-0043 | BLOCKED_BY_ENV | BLOCKED_BY_ENV | BLOCKED_BY_ENV |
| OP-0044 | FAIL | FAIL | FAIL |

## Attempt selection and blockers

- `OP-0014/R2`, `OP-0015/R1-R2`, and `OP-0017/R2`: selected explicit attempt 2; the earlier attempt ended after turn 1 with `Error: disabled`.
- `OP-0025/R1-R3` and `OP-0026/R1-R2`: selected the later unlabeled appended records. Earlier incomplete or failed terminal responses remain described in the CSV `attempt_history` column.
- `OP-0043/R1-R3`: accepted as `BLOCKED_BY_ENV` because every record explicitly states that a true external-link outage was unavailable and used a loopback-refused surrogate. The local answer and public retry do not substitute for the blocked outage/recovery dimension.

## Deterministic corroboration

- All 28 document fixtures were uploaded successfully into the isolated home. Therefore, false not-found results in `OP-0016`, `OP-0025`, `OP-0026`, `OP-0032`, `OP-0033`, and `OP-0039` are functional failures, not environment blockers.
- `OP-0029/R1`, `OP-0030/R1`, and `OP-0034/R1` have real outputs with the required controlled markers and pass their relevant postconditions.
- `OP-0013/R1-R3` created files in the wrong directory. `OP-0029/R2-R3` and `OP-0030/R2-R3` ended in raw tool calls and created no outputs.
- Both `OP-0031` DOCX files open, but neither contains a real DOCX heading style or table; the Markdown source was flattened into plain text.
- All three `OP-0037` target directories remain absent after the recorded approval exchanges.

## Main semantic failure patterns

- Raw tool or transport output reaches the user in `OP-0025`, `OP-0028`, `OP-0029/R2-R3`, `OP-0030/R2-R3`, `OP-0034/R2-R3`, and `OP-0044/R1-R2`.
- Document recall repeatedly fails despite successful indexing.
- Exact output constraints fail in `OP-0007`, `OP-0010`, `OP-0013`, `OP-0019`, and `OP-0024/R2`.
- `OP-0036` reports unmeasured values and later retracts them; `OP-0038` returns raw page content instead of synthesis.
- `OP-0035/R3` is internally contradictory, while `OP-0041/R1-R2` lacks evidence for persona application and restoration; these are `INCONCLUSIVE` rather than `PASS`.

The per-repeat adjudication, all eight rubric scores, selected-attempt history, and evidence basis are recorded in `SEMANTIC_REVIEW_PASS_2.csv`.
