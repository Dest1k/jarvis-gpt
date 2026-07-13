# Evidence and verdict contract

## Evidence record

Every JSONL record is `jarvis.qa.evidence.v1` and binds:

- campaign ID, independent namespace, case ID, requirement flag, and time;
- sanitized request and expected contract;
- deterministic validator specifications;
- sanitized observation and bounded evidence;
- factual assertion objects;
- authoritative deterministic failure names;
- a typed deterministic or runner-classification replay contract;
- case verdict and pre-write redaction count.

Records are append-only. The campaign path and final manifest are
exclusive-create. A process flush and filesystem sync follows every case so a
later harness failure does not erase earlier evidence. Finalization computes a
manifest from the persisted raw bytes. It binds file size and SHA-256, ordered
raw-line digests, a terminal chain digest, case order, counts, and exit code.
The store records exact size, full digest, and ordered line digests during each
append and rejects any pre-seal file change. Finalization returns the SHA-256
of the exact manifest bytes supplied to the exclusive writer for separate
retention; it never trusts a later reopen of that path.
Validation requires this trusted out-of-band anchor plus the paired manifest
and independently recomputes every field. A generic bundle presented without
that anchor fails closed; only the exact committed calibration fixture has a
reviewed repository pin.

Verifier provenance is deliberately not serialized. A self-consistent replay
document or in-memory digest object is not sufficient: replay rechecks the
ordered canonical record hashes, and packet creation reopens the evidence,
checks the trusted manifest anchor, and performs a fresh replay. Persisted replay
reports regain verified status only after the same anchored comparison.
Persisted packets and reviews remain unverified until their complete packet
content is re-derived from anchored evidence. Matching replay fields alone are
insufficient; request, output, bounded evidence, verdicts, failures, and every
source binding must match before adjudication.

## Review citations and independence

Review packets expose digest-bound `evidence:<id>` and `assertion:<id>`
catalogs. A bounded-evidence entry receives an evidence ID only when it is an
exact typed envelope with a supported `kind`, non-empty exact `assertion_ids`
links into the packet catalog, and substantive `content`. Transport, tags,
arbitrary unknown scalar fields, and other context metadata do not become
substantive evidence IDs; a malformed attempted typed envelope fails packet
creation. Every review must cite exact existing IDs; substantive `PASS` or
`FAIL` requires at least one ID from each catalog. Empty, duplicate, wildcard,
parent, and unknown citations fail validation.

Reviews record a context ID, unique run nonce, provider, model, profile, context
digest, and top-level packet digest. Adjudication computes independence only
after each context digest matches the positional anchor retained when that
context was issued and each review digest matches the positional anchor
retained after review completion. The latter binds the semantic verdict,
rationale, citations, context, and complete packet. Anchors derived from the
review files presented for adjudication are not trusted. A missing, mismatched,
swapped, or reused anchor, repeated context ID, or repeated nonce cannot count
as an independent vote and yields `INCONCLUSIVE` unless deterministic replay
already requires `FAIL`. Context/review hashes detect mutation relative to
retained anchors; they do not authenticate the named provider or operator.

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
at runtime. The common output boundary performs recursive key/text redaction,
then bounds values, strictly serializes JSON, and scans the structured and raw
result again. Private-key, session/cookie, CSRF/OAuth/JWT, password/connection,
authorization, and explicit-canary material share that boundary. Unresolved
material prevents file creation. CLI summaries contain counts, paths,
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
Replay binds the actual evidence and manifest byte digests and content-addresses
its own deterministic result. Review packets are created only after a fresh
verified replay and include the raw record, evidence, manifest, and replay
digests; deterministic failures come from replay rather than mutable record
metadata.
For runner-produced `BLOCKED_BY_ENV`, `BLOCKED_BY_SPEC`, optional `SKIP`, and
`ERROR`, replay instead verifies the exact typed runner assertion, its pass/fail
polarity, the requirement flag, and a reason bound to the recorded error before
preserving the classification. A free-form non-replayable marker is not
accepted.

Artifact validators never trust recorded existence or digest claims. They stat
and SHA-256 hash the exact contract-approved artifact path and, when requested,
the explicit source path used for the before/after integrity comparison.

## Evidence limitations

Schema validity proves structure, not truth. A reviewer cannot infer a license,
security result, action completion, or product readiness from a declaration
without referenced evidence. Missing evidence remains `INCONCLUSIVE` or
`BLOCKED`; it never becomes `PASS`.

SHA-256 bindings detect mutation, reorder, truncation, and paired substitution
relative to a previously trusted manifest anchor. They are not signatures and
do not authenticate an author or source.
