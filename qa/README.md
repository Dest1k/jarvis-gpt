# JARVIS assurance harness

`qa/` is a permanent, developer-only assurance layer. It generalizes safe,
reusable invariants from the committed functional campaign
`20260713T002206Z_686424795712`; it has no runtime import from `.audit/**` and
does not start or stop JARVIS.

The three source identities are intentionally separate:

- foundation base: `b2c481de1a9e68079a67ff49790eb685a09e80e5`;
- functional audit completion: `5aae9855f0779c746ec9287c2ec8917637fedb36`;
- production code exercised by the campaign:
  `3fda655e4f723a0d8f58a4edfb4b3ee7dda079fe`.

## Safety boundary

- HTTP base URLs must be loopback. The client uses `trust_env=False`, disables
  redirects, and permits only an explicit `(method, path)` allowlist.
- CLI requests map through typed fixed specs to the resolved absolute Python
  interpreter and an absolute repository-owned launcher. The child uses
  `-I -S`, `shell=False`, repository-root `cwd`, and a minimal environment
  without `PATH`, `PYTHONPATH`, `PYTHONHOME`, user-site, or startup hooks.
- Every campaign receives a timestamp-and-random campaign ID and a separate
  namespace.
- Evidence files and manifests use exclusive-create. JSONL is flushed and
  synced after every case, then finalized once. The manifest binds the exact
  evidence bytes, size, ordered raw-line digests, terminal chain digest,
  verdict counts, and campaign exit code. The store tracks those bytes as they
  are appended and refuses to finalize if the open file changed; the returned
  manifest anchor hashes the exact bytes supplied to the exclusive writer.
- Evidence validation requires the paired manifest plus a manifest SHA-256
  retained out of band when the bundle is finalized. Replay independently
  recomputes both raw-file digests and its own deterministic digest. Review
  packets can be built only from a fresh verified replay and carry the record,
  evidence, manifest, and replay SHA-256 bindings. The committed calibration
  fixture is the only bundle with a repository-reviewed built-in anchor.
  Deserialized integrity/replay fields are structural claims only; packet
  creation reopens the anchored evidence and performs a new deterministic
  replay before it accepts them. Deserialized packets and reviews likewise
  remain unverified until adjudication re-derives the complete immutable packet
  from anchored evidence; a matching replay digest alone grants no provenance.
- Review packets publish exact substantive evidence and assertion citation IDs.
  A substantive bounded-evidence item is an exact typed envelope containing
  `kind`, linked `assertion_ids`, and non-metadata `content`; arbitrary unknown
  fields remain uncitable metadata. Each review binds a canonical context ID,
  unique run nonce, provider, model, profile, context digest, and packet digest.
  Retain each context digest out of band when the context is issued, then retain
  each completed review digest before its result enters untrusted storage.
  Adjudication requires both positional anchor pairs and computes pairwise
  independence from the anchored facts and review outputs;
  missing/mismatched/reused anchors, repeated contexts/nonces, and metadata-only
  evidence cannot produce semantic `PASS`. Review/context hashes detect mutation
  relative to retained anchors but do not authenticate the named provider.
- One output boundary recursively redacts credential-bearing keys and text,
  private/session/cookie/OAuth/JWT/password/connection material, and explicit
  disposable canaries before bounding and strict serialization. A post-scan
  fails closed before a file is created if any material remains.
- A `PASS` needs at least one factual assertion. A deterministic failure cannot
  be promoted by semantic review.
- Scenario and validator control objects reject unknown fields and wrong types;
  JSON contract schemas are themselves validated before any instance result.
- Artifact checks accept only canonical relative paths plus trusted out-of-band
  root aliases. Descriptor-based bounded reads reject absolute/traversal,
  missing, special, oversized, symlink, junction, and reparse targets; recorded
  `exists` and hash fields remain untrusted claims.
- Path-derived IDs and generated output leaves are canonical, resolved beneath
  their exact output root, reparse-safe, and exclusive-create.
- Runner-produced blocked, skipped, and error records carry a typed
  classification replay contract with an exact runner assertion and reason.
- This first version never manages JARVIS lifecycle, Docker, models, runtime
  state, or external network resources.

## Commands

Run from the repository root with the existing Python 3.11 environment:

```powershell
py -3.11 -m qa.cli validate-suite qa\suites\operator_core
py -3.11 -m qa.cli validate-evidence qa\tests\fixtures\calibration_evidence.jsonl
py -3.11 -m qa.cli replay qa\tests\fixtures\calibration_evidence.jsonl
py -3.11 -m qa.cli validate-evidence <evidence.jsonl> --expected-manifest-sha256 <retained-sha256>
py -3.11 -m qa.cli replay <evidence.jsonl> --expected-manifest-sha256 <retained-sha256>
py -3.11 -m qa.cli build-review-packets <evidence.jsonl> --expected-manifest-sha256 <retained-sha256> --output-dir <new-directory>
py -3.11 -m qa.cli adjudicate <review-1.json> <review-2.json> --replay <replay.json> --evidence <evidence.jsonl> --context-anchor-1 <retained-review-1-context-sha256> --context-anchor-2 <retained-review-2-context-sha256> --review-anchor-1 <retained-review-1-sha256> --review-anchor-2 <retained-review-2-sha256> --expected-manifest-sha256 <retained-manifest-sha256> --output <new-file.json>
py -3.11 -m qa.cli run-suite <suite-directory> --output-root <new-directory>
```

`run-suite` accepts `--base-url` only for loopback scenarios. Without it, HTTP
cases return `BLOCKED_BY_ENV`; offline cases remain usable. Review packet,
review result, adjudication, replay-report, evidence, and manifest writers
refuse overwrite. `run-suite` emits `manifest_sha256`; retain that value
separately before the evidence bundle enters untrusted storage or transport.
Retain each context digest when the context is issued and each review digest
immediately after the review is completed, before the review result enters
untrusted storage or transport. Pass both pairs in the same positional order as
the review files. Recomputing any anchor from the objects presented for
adjudication does not establish trust. `adjudicate` reopens the anchored
evidence, performs a fresh replay, and requires every persisted packet
field—including request, output, and bounded evidence—to equal the newly
derived packet before either review can influence a verdict.

Runner exit codes are:

| Code | Meaning |
| ---: | --- |
| `0` | All required cases passed. |
| `1` | At least one deterministic case failed. |
| `2` | No deterministic failure, but required work is incomplete or inconclusive. |
| `3` | Harness/configuration/recording error. |

`replay` returns zero when recomputed verdicts exactly match the immutable
recorded verdicts, even when the calibrated corpus intentionally includes
known `FAIL` and `INCONCLUSIVE` cases. Deterministic records rerun validators;
typed runner classifications validate and preserve `BLOCKED_BY_ENV`,
`BLOCKED_BY_SPEC`, optional `SKIP`, and `ERROR` without a free-form bypass.
`adjudicate` returns `0`, `1`, or `2` for `PASS`, `FAIL`, or `INCONCLUSIVE`
respectively.

SHA-256 binding detects mutation and substitution relative to a previously
trusted anchor; it is integrity evidence, not signer authentication.

## Layout

- `models.py`, `runner.py`, `evidence.py`, `redaction.py`, `output.py`: campaign
  execution, content-bound evidence, and the common safe-output boundary.
- `validators/`: exact format/JSON, response, stream, artifact, identity,
  claimed-state, canary, and exit-result checks.
- `review/`: immutable packets, explicit independence labels, separate review
  outputs, and fail-closed adjudication.
- `replay.py`: offline deterministic replay of sanitized JSONL.
- `upstream/`: offline provenance/adoption gate.
- `schemas/`: machine-readable scenario, evidence, manifest, replay, review,
  packet, adjudication, and verdict contracts.
- `suites/`: permanent scenario namespaces. The initial committed suite is
  intentionally small; remediation work adds task-specific scenarios here.
- `tests/fixtures/calibration_evidence.jsonl`: sanitized, offline calibration
  derived from committed campaign findings. It contains no runtime credential.
- `tests/fixtures/calibration_evidence.manifest.json`: raw-byte integrity
  binding for that calibration corpus.

## Validation

```powershell
py -3.11 -m pytest qa\tests -q
py -3.11 -m ruff check qa
py -3.11 -m compileall qa
git diff --check
```

No command above needs a running JARVIS instance, Docker, a model, an API key,
or external network access.
