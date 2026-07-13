# Assurance architecture

## Purpose

The assurance foundation is an isolated developer toolchain between completed
functional assessment and production remediation. It validates evidence and
decision contracts; it does not repair product behavior or declare the product
ready.

```text
scenario JSON
    -> safe executor (offline | allowlisted loopback HTTP | allowlisted CLI)
    -> deterministic validators
    -> common redact/bound/serialize/post-scan boundary
    -> append-only JSONL + raw-byte-bound exclusive manifest
    -> independently content-bound offline replay
    -> isolated review packet A / review packet B
    -> fail-closed adjudication
```

Upstream adoption is a parallel offline gate:

```text
candidate + provenance + bounded local evidence
    -> schema/field/license/review/hash checks
    -> PASS | FAIL | BLOCKED
    -> separate human decision
```

## Trust boundaries

### Execution

`qa.runner` accepts only loopback HTTP base URLs. `httpx.Client` is constructed
with `trust_env=False` and redirects disabled. Routes are exact allowlist
entries. CLI requests map through typed immutable specs to the resolved
absolute interpreter and a repository-owned absolute launcher. The child uses
isolated/no-site startup, `subprocess.run(..., shell=False)`, repository-root
`cwd`, a bounded timeout, and a minimal environment that omits process/import
search paths and Python startup hooks. The runner has no lifecycle operation
and never starts/stops JARVIS, Docker, models, or external services.

### Campaign isolation

`CampaignIdentity.create()` generates distinct campaign and namespace values
from UTC time plus a random nonce. `EvidenceStore` creates a new JSONL path in
exclusive mode, appends after each case, flushes and syncs each record, and
creates its manifest exclusively. Reusing a campaign path is an error.

### Evidence

Validators see the bounded observation needed to produce factual assertions.
Before persistence, every generated JSON output passes one boundary that
recursively sanitizes credential-bearing keys and text, including private-key,
refresh/session/cookie, CSRF/OAuth/JWT, password/connection, bearer, and
explicit disposable-canary material. Values are bounded only after redaction,
strictly serialized, then scanned again. Unresolved material prevents output
creation. Evidence, manifests, replay reports, review packets/results,
adjudications, CLI JSON, and upstream-validator diagnostics use this boundary.

The campaign manifest binds the exact raw JSONL bytes, ordered raw-line
digests, terminal chain, counts, and exit code. Finalization first compares the
open file with the exact size, full digest, and ordered line digests recorded by
each append, then derives the retained manifest anchor from the exact bytes
given to the exclusive writer rather than reopening the path. Replay recomputes
the evidence and manifest digests from the actual files, compares the manifest
digest with that out-of-band anchor, and content-addresses its own result. A
self-consistent evidence/manifest pair is therefore insufficient:
paired substitution fails against the retained anchor. Generic bundles have no
implicit trust; the committed sanitized calibration fixture is the sole exact
repository path with a reviewed built-in pin. A review packet is created only
by the workflow that just completed that verified replay; it carries source
record/evidence/manifest/replay digests and uses replayed deterministic
failures. These hashes detect mutation and substitution relative to the trusted
anchor but do not authenticate a signer.

Integrity and replay provenance are process-local verifier results and are not
serialized as trusted booleans. A loaded replay report is structural data until
it is compared with a new replay of the anchored evidence. The packet factory
performs that verification itself and also rebinds the ordered canonical record
hashes, so caller-modified records or self-asserted digest objects cannot mint a
verified packet. Packet JSON content is recursively immutable after creation.
A deserialized packet or review is still unverified: replay-field agreement
alone grants no provenance. Before adjudication, the verifier reopens anchored
evidence, performs a fresh replay, re-derives the complete packet, and requires
exact equality of every field, including request, output, and bounded evidence.

### Verdict authority

Deterministic validators are primary. An empty assertion set is a harness
error, and an empty `PASS` is rejected by the typed model. Semantic reviewers
cannot edit files, runtime, or evidence. Each review binds a typed factual
context, packet digest, and exact packet citation IDs. Pairwise independence is
computed from distinct context IDs/nonces and actual provider/model/profile
differences; no review-supplied level is trusted. Each context digest must match
its positional anchor retained out of band when the context was issued, and
each completed review digest must match the positional anchor retained before
the result entered untrusted storage. The review anchor binds the verdict,
rationale, citations, context, and complete packet. Missing, mismatched,
swapped, or reused anchors make independence unverifiable. Only exact typed
bounded-evidence envelopes with linked assertion IDs and substantive content
receive evidence IDs; arbitrary and metadata-only fields remain uncitable.
Semantic `PASS`/`FAIL` requires both an evidence and assertion citation. Adjudication
rechecks review digests and embeds both immutable outputs. A deterministic
failure always yields `FAIL`; repeated or unverifiable contexts, disagreement,
or missing evidence yield `INCONCLUSIVE`. These digests detect mutation relative
to retained anchors but do not authenticate provider identity.

## Validators

The initial registry provides:

- exact text, regular expression, language, word/line count, JSON parsing, and
  a bounded JSON Schema subset;
- forbidden `call:`/tool/function/role/protocol/traceback/transport/internal
  markers, plus empty/duplicate/truncated final detection;
- NDJSON object parsing, known event types, single leading metadata, single
  final terminal event, delta reconstruction, persistence equality, and error
  event rejection;
- exact artifact path, existence, SHA-256, and source-unchanged checks;
- conversation/runtime/transcript/namespace isolation;
- claimed action versus observed state;
- pre-write canary/credential absence;
- process exit code versus machine-readable result consistency.

Unknown validators fail closed. Validator exceptions become failed assertions,
never `PASS`.

Scenario and validator control objects use strict schemas: unknown fields,
wrong primitive/container types, malformed schema keywords, and `bool` used as
an integer all fail deterministically. Artifact validators receive canonical
allowed roots and byte caps only through a trusted out-of-band validation
context. Target and optional source paths are independent relative paths;
bounded descriptor reads reject escapes, reparse points, non-regular files,
and oversize content before exposing any digest.

## Provenance

The implementation generalizes invariants from the committed sanitized
functional harness and reports under run `20260713T002206Z_686424795712`.
There is deliberately no runtime import from `.audit/**`, no dependency on a
retained runtime home, and no copy of raw evidence. Calibration references
finding classes, not local credential-bearing artifacts.

Relevant immutable identities:

| Role | SHA |
| --- | --- |
| Foundation base/orchestration HEAD | `b2c481de1a9e68079a67ff49790eb685a09e80e5` |
| Functional audit completion | `5aae9855f0779c746ec9287c2ec8917637fedb36` |
| Production source exercised | `3fda655e4f723a0d8f58a4edfb4b3ee7dda079fe` |

These roles are not interchangeable.
