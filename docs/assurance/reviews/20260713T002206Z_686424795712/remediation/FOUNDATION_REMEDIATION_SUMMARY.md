# Assurance foundation remediation summary

## Result

The isolated remediation campaign closes confirmed blockers B01-B14 and is in
state `ASSURANCE_FOUNDATION_REMEDIATION_CANDIDATE_FOR_REVIEW`.

- Base: `7d4a4757df9aa3264fd16caf439e40588c375fee`
- Branch: `fix/assurance-foundation/20260713T002206Z_686424795712`
- Final implementation commit: the commit containing this document; resolve
  with `git rev-parse HEAD` in the remediation worktree.
- This state is a candidate for independent review, not an attestation and not
  product readiness.

## Blocker closure

| Batch | Blockers | Commit | Result |
| --- | --- | --- | --- |
| A | B01, B02, B04, B06 | `e9a316de16f6c7d4ea1f595417846d30b2041e33` | PASS |
| B | B03, B05 | `ddf282aee94b6af27aeab2e0fc117303ba1a203a` | PASS |
| C | B07 | `593f5805de7a2c71a85fa282a360876532cacf68` | PASS |
| D | B08-B11 | `7aa54c9097dacfa01909d0f5a43c745f27e43a1e` | PASS |
| E | B12-B14 | this commit (`assurance: bind remediation sources and reviewed wave inputs`) | PASS |

The detailed code, regression, command, and residual-risk mapping is in
`BLOCKER_MATRIX.csv`.

## Final assurance evidence

- Complete QA suite: `218 passed, 3 skipped`.
- Dedicated B12-B14 integrity suite: `33 passed`.
- Ruff, compileall, and `git diff --check`: PASS.
- Calibration: 8/8 replayed, 1 PASS, 6 FAIL, 1 INCONCLUSIVE, 0 mismatches.
- Overlay verifier: 4/4 immutable Git-blob pins and 17/17 mappings/task blobs.
- Review packet construction: 8 packets; anchored adjudication PASS with
  verified independence and review anchors.
- Offline upstream validation: PASS.
- Main `.audit` before/after manifests: 544 entries each, identical retained
  SHA-256, zero differences, and a fresh live-tree comparison.
- Independent scoped reviews returned GO for B01-B14. The final B12 parser
  review rejected the previously confirmed shadow-structure bypass and 15
  adjacent noncanonical forms.

The three skips are host-capability real-symlink cases on Windows. Deterministic
simulated reparse coverage and independent reparse-boundary checks passed.

## Scope and protected state

No production source, `.audit/**`, dependency, runtime state, main checkout,
candidate branch, or review branch was changed. No JARVIS, Docker, dispatcher,
model, external network, push, merge, `functional/READY`, or production
remediation task was used. Generated validation data contains only sanitized
fixtures, disposable canaries, and metadata-only manifests in the external
backup root.

Independent review must resolve the exact final commit, verify the external
anchors, rerun the commands in `VALIDATION_REPORT.md`, and issue any foundation
attestation separately.
