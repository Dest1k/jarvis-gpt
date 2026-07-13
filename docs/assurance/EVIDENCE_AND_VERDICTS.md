# Evidence and verdict contract

## Evidence record

Every JSONL record is `jarvis.qa.evidence.v1` and binds:

- campaign ID, independent namespace, case ID, requirement flag, and time;
- sanitized request and expected contract;
- deterministic validator specifications;
- sanitized observation and bounded evidence;
- factual assertion objects;
- authoritative deterministic failure names;
- case verdict and pre-write redaction count.

Records are append-only. The campaign path and final manifest are
exclusive-create. A process flush and filesystem sync follows every case so a
later harness failure does not erase earlier evidence.

## Statuses

| Verdict | Use |
| --- | --- |
| `PASS` | At least one factual assertion exists and every assertion passes. |
| `FAIL` | One or more deterministic assertions fail. |
| `INCONCLUSIVE` | Deterministic checks pass, but semantic evidence or agreement is insufficient. |
| `BLOCKED_BY_ENV` | A required external/local environment capability is unavailable. |
| `BLOCKED_BY_SPEC` | The case cannot execute safely because its contract is incomplete or disallowed. |
| `SKIP` | An explicitly optional case was not executed. |
| `ERROR` | Harness, parser, validator, or recording failure. |

Exit-code precedence is `ERROR -> 3`, `FAIL -> 1`, required incomplete status
`-> 2`, otherwise `PASS -> 0`. A deterministic `FAIL` cannot be downgraded or
promoted by a model reviewer.

## Sanitization

Real runtime credentials are prohibited. Tests construct disposable canaries
at runtime. Key-aware and textual redaction runs before serialization and
records only redaction event paths/reasons. Evidence validation rejects
credential-like values that remain. CLI summaries contain counts, paths,
verdicts, and bounded redacted diagnostics; they do not echo observations.

## Replay calibration

`qa/tests/fixtures/calibration_evidence.jsonl` is a committed sanitized corpus
derived from the completed campaign reports. It exercises:

- one passing NDJSON reconstruction case;
- raw tool-envelope leakage;
- empty/duplicate/truncated final state;
- a sanitized pre-write canary detection event;
- exit-code/machine-result mismatch;
- exact artifact path/hash/source mismatch;
- cross-runtime transcript mismatch;
- one semantic case with insufficient evidence (`INCONCLUSIVE`).

Replay recomputes deterministic assertions and compares the new verdict with
the recorded verdict. The replay command succeeds only when every verdict
matches. It does not reinterpret a known calibrated `FAIL` as harness failure.

## Evidence limitations

Schema validity proves structure, not truth. A reviewer cannot infer a license,
security result, action completion, or product readiness from a declaration
without referenced evidence. Missing evidence remains `INCONCLUSIVE` or
`BLOCKED`; it never becomes `PASS`.
