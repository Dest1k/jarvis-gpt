# Profile Semantic Review Pass 2 — OP-0045..OP-0068

## Scope and method

- Independently reviewed 24 catalog cases and exactly 62 required `(case, repeat)` keys.
- Primary semantic evidence: `OPERATOR_TASK_CATALOG.csv` and `evidence/gui_operator_runs.jsonl`.
- Corroborating evidence was limited to the new mono/mono-perf direct probes, startup snapshots, and profile fixture upload/QA records.
- Did not read pass 1, any previous semantic review, reconciliation output, old queues, or old findings.
- Removed only the leading UI role and timing metadata before judgment. Empty responses, router fallbacks, and other visible text remained part of the result.
- Selected the authoritative complete retry for `OP-0045/R1` and `OP-0051/R1-R3`; all other keys had one authoritative record.

## Rubric

The CSV scores all eight catalog dimensions:

- `2` — fully met or directly observed.
- `1` — partial result or material uncertainty.
- `0` — materially failed.
- `NA` — not exercised or not observable.

## Accepted status summary

| Profile | Repeats | PASS | FAIL | INCONCLUSIVE | BLOCKED_BY_ENV | BLOCKED_BY_SPEC | UNSUPPORTED |
|---|---:|---:|---:|---:|---:|---:|---:|
| gemma4-mono-perf | 31 | 0 | 31 | 0 | 0 | 0 | 0 |
| gemma4-mono | 31 | 0 | 26 | 0 | 5 | 0 | 0 |
| **Total** | **62** | **0** | **57** | **0** | **5** | **0** | **0** |

| Case | R1 | R2 | R3 |
|---|---|---|---|
| OP-0045 | FAIL | FAIL | — |
| OP-0046 | FAIL | FAIL | — |
| OP-0047 | FAIL | FAIL | FAIL |
| OP-0048 | FAIL | FAIL | FAIL |
| OP-0049 | FAIL | FAIL | — |
| OP-0050 | FAIL | FAIL | — |
| OP-0051 | FAIL | FAIL | FAIL |
| OP-0052 | FAIL | FAIL | FAIL |
| OP-0053 | FAIL | FAIL | FAIL |
| OP-0054 | FAIL | FAIL | FAIL |
| OP-0055 | FAIL | FAIL | FAIL |
| OP-0056 | FAIL | FAIL | — |
| OP-0057 | FAIL | FAIL | — |
| OP-0058 | FAIL | FAIL | — |
| OP-0059 | FAIL | FAIL | FAIL |
| OP-0060 | FAIL | FAIL | FAIL |
| OP-0061 | FAIL | FAIL | — |
| OP-0062 | BLOCKED_BY_ENV | BLOCKED_BY_ENV | — |
| OP-0063 | BLOCKED_BY_ENV | BLOCKED_BY_ENV | BLOCKED_BY_ENV |
| OP-0064 | FAIL | FAIL | FAIL |
| OP-0065 | FAIL | FAIL | FAIL |
| OP-0066 | FAIL | FAIL | FAIL |
| OP-0067 | FAIL | FAIL | FAIL |
| OP-0068 | FAIL | FAIL | — |

## Profile-level evidence

### gemma4-mono-perf

- The backend health and model catalog identify `gemma4-mono-perf`, `/models/gemma4-31b-it-nvfp4`, and a configured 4096-token context.
- Three direct chat probes returned HTTP 200 in 6.97–7.33 seconds, but every response was the same repeated `cyclic` token sequence and terminated by length.
- GUI attempts therefore represent functional failures, not missing-compute environment blockers: they either returned the LLM-router 400 fallback, timed out empty, or failed indexed document recall.
- All 14 profile fixtures were uploaded successfully. The mono-perf PDF QA passed, so `OP-0050` and `OP-0051` are not blocked by missing or malformed test inputs.

### gemma4-mono

- The bounded 20-minute startup deadline expired while the dispatcher remained `starting` and `/v1/models` was unavailable.
- The dispatcher became healthy only after about 27 minutes. The startup evidence records the first GUI request running for minutes at roughly 0.1–0.4 tokens per second after the UI timeout.
- The post-readiness direct probe took 47.254 seconds and again returned only repeated `cyclic` tokens. Cases run both before and after readiness produced empty terminal results.
- These are accepted as `FAIL` where all required inputs existed: model assets and the provider were eventually present, while bounded startup, latency, output quality, and user-visible completion failed.
- `OP-0062/R1-R2` and `OP-0063/R1-R3` are `BLOCKED_BY_ENV`: the required `mono-doc-*`, `mono-bad-*`, and `mono-good-*` inputs are absent from the complete 14-file profile upload inventory. The simultaneous runtime timeouts are recorded as secondary failures but cannot replace the missing precondition.
- The indexed `mono-mission-a-*` and `mono-mission-b-*` inputs do exist, so `OP-0064` is a functional failure rather than an environment block.

## Attempt selection

- `OP-0045/R1`: JSONL line 121, explicit attempt 2; line 120 was empty and incomplete.
- `OP-0051/R1-R3`: JSONL lines 149–151, explicit attempt 2; lines 146–148 ended after turn 1 with `Error: disabled`.
- Exact JSONL line numbers, supporting evidence references, concise rationales, and all eight dimension scores are in `PROFILE_SEMANTIC_REVIEW_PASS_2.csv`.
