# Profile Semantic Review Pass 1 — OP-0045..OP-0068

## Scope and method

- Independent review of exactly 62 unique case-repeat keys across `gemma4-mono-perf` and `gemma4-mono`.
- Semantic inputs: `OPERATOR_TASK_CATALOG.csv` and `gui_operator_runs.jsonl`.
- Profile-condition evidence: direct model probes and bounded startup snapshots from the same functional namespace.
- Fixture presence was checked only to distinguish a missing-input specification blocker from a profile runtime blocker.
- No other semantic pass, reconciliation output, old static queue, or old findings index was read.
- Authoritative selection is the latest complete non-error attempt. If none exists, the latest/sole incomplete attempt is retained and cannot receive PASS.
- Visible GUI metadata such as `ASSISTANT` and standalone timing text was removed before judgment.
- Scores use 0 = failed/contradicted, 1 = partial or unobservable under the blocker, 2 = observed satisfied.

## Result

| Status | Repeats |
| --- | ---: |
| PASS | 0 |
| FAIL | 2 |
| BLOCKED_BY_ENV | 55 |
| BLOCKED_BY_SAFETY | 0 |
| BLOCKED_BY_SPEC | 5 |
| INCONCLUSIVE | 0 |
| NOT_APPLICABLE | 0 |
| Total | 62 |

## Authoritative attempt notes

- OP-0045/1 selects complete attempt 2 at JSONL line 121 over the earlier empty `final=false` record at line 120.
- OP-0051/1..3 select complete attempt 2 at lines 149..151. Lines 146..148 ended after turn 1 with `Error: disabled` and remain documented in the CSV.
- Ten keys have complete non-error records: OP-0045/1..2, OP-0048/1..3, OP-0050/1..2, and OP-0051/1..3. Every other authoritative record is incomplete.

## Profile blockers

### gemma4-mono-perf

- The isolated health/profile/model identity was correct: `gemma4-mono-perf`, 31B model, configured 4096-token context.
- All three direct probes returned HTTP 200 in roughly 7 seconds but emitted only repeated `cyclic` tokens and stopped by length.
- GUI requests therefore either produced a corroborated 400 router fallback or expired with no semantic answer.
- The complete OP-0050 records are treated as FAIL rather than environment-blocked because the indexed documents were available and the final responses specifically asserted that they could not be found.

### gemma4-mono

- Dispatcher remained `starting` through the campaign's 20-minute bound and became healthy only after about 27 minutes.
- Startup evidence says the first GUI request continued for minutes at roughly 0.1–0.4 token/s after the UI timeout.
- A ready direct probe took 47.3 seconds for 16 completion tokens and still emitted only repeated `cyclic` tokens.
- This makes all answer-bearing semantic dimensions unobservable within the captured GUI windows.

## Specification blockers

- OP-0062 requests attached `mono-doc-{repeat}.txt`, but no matching fixtures/uploads exist.
- OP-0063 requests `mono-bad-{repeat}.pdf` and `mono-good-{repeat}.pdf`, but only the distinct `mono-perf-bad/good` fixtures were supplied.
- These five repeats are BLOCKED_BY_SPEC even though the mono runtime was also degraded.

## Per-case repeat status

Legend: P = PASS, F = FAIL, E = BLOCKED_BY_ENV, Y = BLOCKED_BY_SAFETY, S = BLOCKED_BY_SPEC, I = INCONCLUSIVE, N = NOT_APPLICABLE.

| Case | Repeats |
| --- | --- |
| OP-0045 | E / E |
| OP-0046 | E / E |
| OP-0047 | E / E / E |
| OP-0048 | E / E / E |
| OP-0049 | E / E |
| OP-0050 | F / F |
| OP-0051 | E / E / E |
| OP-0052 | E / E / E |
| OP-0053 | E / E / E |
| OP-0054 | E / E / E |
| OP-0055 | E / E / E |
| OP-0056 | E / E |
| OP-0057 | E / E |
| OP-0058 | E / E |
| OP-0059 | E / E / E |
| OP-0060 | E / E / E |
| OP-0061 | E / E |
| OP-0062 | S / S |
| OP-0063 | S / S / S |
| OP-0064 | E / E / E |
| OP-0065 | E / E / E |
| OP-0066 | E / E / E |
| OP-0067 | E / E / E |
| OP-0068 | E / E |

Detailed selected line numbers, rubric scores, prior-attempt notes, and evidence references are in `PROFILE_SEMANTIC_REVIEW_PASS_1.csv`.
