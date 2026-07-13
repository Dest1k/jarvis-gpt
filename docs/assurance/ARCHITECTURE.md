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
    -> pre-write redaction
    -> append-only JSONL + exclusive manifest
    -> offline replay
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
Before persistence, evidence is recursively sanitized for credential-bearing
keys, textual assignments, bearer credentials, and explicitly supplied
disposable canaries. Sanitized values are bounded only after redaction, so
truncation cannot expose an unprocessed suffix. Reviewers receive only the
sanitized request, expected contract, actual output, bounded evidence, and
authoritative deterministic failures.

### Verdict authority

Deterministic validators are primary. An empty assertion set is a harness
error, and an empty `PASS` is rejected by the typed model. Semantic reviewers
cannot edit files, runtime, or evidence. Adjudication embeds both immutable
review outputs. A deterministic failure always yields `FAIL`; disagreement or
missing evidence yields `INCONCLUSIVE`.

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
