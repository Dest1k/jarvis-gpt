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
- CLI commands are exact argument tuples and execute with `shell=False`.
- Every campaign receives a timestamp-and-random campaign ID and a separate
  namespace.
- Evidence files and manifests use exclusive-create. JSONL is flushed and
  synced after every case; old runs are never overwritten.
- Recursive key, text, bearer, and disposable-canary redaction runs before
  bounded evidence is serialized.
- A `PASS` needs at least one factual assertion. A deterministic failure cannot
  be promoted by semantic review.
- This first version never manages JARVIS lifecycle, Docker, models, runtime
  state, or external network resources.

## Commands

Run from the repository root with the existing Python 3.11 environment:

```powershell
py -3.11 -m qa.cli validate-suite qa\suites\operator_core
py -3.11 -m qa.cli validate-evidence qa\tests\fixtures\calibration_evidence.jsonl
py -3.11 -m qa.cli replay qa\tests\fixtures\calibration_evidence.jsonl
py -3.11 -m qa.cli build-review-packets <evidence.jsonl> --output-dir <new-directory>
py -3.11 -m qa.cli adjudicate <review-1.json> <review-2.json> --output <new-file.json>
py -3.11 -m qa.cli run-suite <suite-directory> --output-root <new-directory>
```

`run-suite` accepts `--base-url` only for loopback scenarios. Without it, HTTP
cases return `BLOCKED_BY_ENV`; offline cases remain usable. Review packet,
review result, adjudication, evidence, and manifest writers refuse overwrite.

Runner exit codes are:

| Code | Meaning |
| ---: | --- |
| `0` | All required cases passed. |
| `1` | At least one deterministic case failed. |
| `2` | No deterministic failure, but required work is incomplete or inconclusive. |
| `3` | Harness/configuration/recording error. |

`replay` returns zero when recomputed verdicts exactly match the immutable
recorded verdicts, even when the calibrated corpus intentionally includes
known `FAIL` and `INCONCLUSIVE` cases. `adjudicate` returns `0`, `1`, or `2`
for `PASS`, `FAIL`, or `INCONCLUSIVE` respectively.

## Layout

- `models.py`, `runner.py`, `evidence.py`, `redaction.py`: campaign execution
  and append-only evidence.
- `validators/`: exact format/JSON, response, stream, artifact, identity,
  claimed-state, canary, and exit-result checks.
- `review/`: immutable packets, explicit independence labels, separate review
  outputs, and fail-closed adjudication.
- `replay.py`: offline deterministic replay of sanitized JSONL.
- `upstream/`: offline provenance/adoption gate.
- `schemas/`: machine-readable scenario, evidence, review, and verdict
  contracts.
- `suites/`: permanent scenario namespaces. The initial committed suite is
  intentionally small; remediation work adds task-specific scenarios here.
- `tests/fixtures/calibration_evidence.jsonl`: sanitized, offline calibration
  derived from committed campaign findings. It contains no runtime credential.

## Validation

```powershell
py -3.11 -m pytest qa\tests -q
py -3.11 -m ruff check qa
py -3.11 -m compileall qa
git diff --check
```

No command above needs a running JARVIS instance, Docker, a model, an API key,
or external network access.
