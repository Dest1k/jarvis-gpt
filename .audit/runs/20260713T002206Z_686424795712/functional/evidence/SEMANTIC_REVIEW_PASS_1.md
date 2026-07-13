# Semantic Review Pass 1 — gemma4-turbo OP-0001..OP-0044

## Scope and method

- Independent pass over 107 repeat records from the new functional namespace.
- Semantic source set: `OPERATOR_TASK_CATALOG.csv` and `gui_operator_runs.jsonl`.
- Selection rule: latest complete, non-error record per `(case, repeat)`; when none exists, the sole/latest incomplete record is retained and cannot receive PASS.
- Leading GUI metadata (`ASSISTANT` and timing) was removed before judgment.
- New-namespace upload records, controlled fixtures, isolated API baseline, and isolated output files were consulted only for deterministic facts such as exact values, path/file existence, hashes, and document structure.
- Old audit artifacts, the old live queue, extended PHASE B/C prompts, and pass 2 outputs were not read.
- Scores use 0 = failed/contradicted, 1 = partial/uncertain, 2 = satisfied. Non-triggered state/recovery dimensions score 2 when no adverse evidence exists.
- Accepted statuses are per repeat, not per case.

## Result

| Status | Repeats |
| --- | ---: |
| PASS | 48 |
| FAIL | 51 |
| INCONCLUSIVE | 4 |
| BLOCKED_BY_ENV | 4 |
| BLOCKED_BY_SPEC | 0 |
| UNSUPPORTED | 0 |
| Total | 107 |

## Attempt-selection notes

- Four `Error: disabled` partial journeys were superseded by later complete attempt 2 records: OP-0014/2, OP-0015/1, OP-0015/2, and OP-0017/2. The earlier records remain referenced in the CSV.
- OP-0025 and OP-0026 each have earlier complete records; the later complete records were selected as required. OP-0025/2 notably regressed from a grounded summary to a raw tool call.
- OP-0040/1..3 have no complete record. Repeat 1 is a clear semantic failure; repeats 2 and 3 remain INCONCLUSIVE despite coherent reconstructed text because the records are `final=false`.

## High-signal findings

- Strong basic-language and state-isolation behavior: OP-0001..0005, OP-0008..0009, OP-0011..0012, OP-0017..0022, OP-0027, OP-0035/1..2, and OP-0042.
- Repeated raw internal/tool leakage: OP-0025, OP-0028, OP-0029/2..3, OP-0030/2..3, OP-0034/2..3, OP-0038, OP-0043, and OP-0044/1..2.
- Persistent document-retrieval failures despite indexed sources: OP-0016, OP-0026, OP-0032, and OP-0039.
- Artifact checks: OP-0029/1, OP-0030/1, and OP-0034/1 are real passes. OP-0013 writes to the wrong directory. OP-0031 creates openable DOCX files but preserves Markdown syntax as plain text, with no Word table or heading style.
- OP-0033/1..2 are environment-blocked by identical corrupt fixture deduplication into one file id and loss of the earlier filename aliases. OP-0033/3 is a model/runtime failure because the surviving alias exists.
- OP-0037/2..3 reach explicit UI approval tokens but the campaign has no matching UI approval event; both remain environment-blocked and no directories were created.
- OP-0041 recalls the visible fact but remains inconclusive because persona application/restoration and namespaced persistence are not observable in the allowed semantic evidence.

## Per-case repeat status

Legend: P = PASS, F = FAIL, I = INCONCLUSIVE, E = BLOCKED_BY_ENV, S = BLOCKED_BY_SPEC, U = UNSUPPORTED.

| Case | Repeats |
| --- | --- |
| OP-0001 | P / P |
| OP-0002 | P / P |
| OP-0003 | P / P |
| OP-0004 | P / P |
| OP-0005 | P / P / P |
| OP-0006 | F / F |
| OP-0007 | F / F |
| OP-0008 | P / P |
| OP-0009 | P / P / P |
| OP-0010 | F / F |
| OP-0011 | P / P |
| OP-0012 | P / P |
| OP-0013 | F / F / F |
| OP-0014 | F / F |
| OP-0015 | P / P |
| OP-0016 | F / F |
| OP-0017 | P / P / P |
| OP-0018 | P / P |
| OP-0019 | P / P |
| OP-0020 | P / P / P |
| OP-0021 | P / P / P |
| OP-0022 | P / P |
| OP-0023 | F / F |
| OP-0024 | P / F |
| OP-0025 | F / F / F |
| OP-0026 | F / F |
| OP-0027 | P / P |
| OP-0028 | F / F |
| OP-0029 | P / F / F |
| OP-0030 | P / F / F |
| OP-0031 | F / F |
| OP-0032 | F / F |
| OP-0033 | E / E / F |
| OP-0034 | P / F / F |
| OP-0035 | P / P / F |
| OP-0036 | F / F |
| OP-0037 | F / E / E |
| OP-0038 | F / F / F |
| OP-0039 | F / F / F |
| OP-0040 | F / I / I |
| OP-0041 | I / I |
| OP-0042 | P / P / P |
| OP-0043 | F / F / F |
| OP-0044 | F / F / F |

Detailed evidence, scores, selected line numbers, and prior-attempt notes are in `SEMANTIC_REVIEW_PASS_1.csv`.
