# Independent foundation re-review input

This file contains sanitized handoff facts only. It is not a foundation
attestation and does not define `REVIEWED_FOUNDATION_COMMIT`.

## Git input

- Base: `7d4a4757df9aa3264fd16caf439e40588c375fee`.
- Branch: `fix/assurance-foundation/20260713T002206Z_686424795712`.
- Final remediation commit: the exact commit containing this file. Resolve it
  in the isolated remediation worktree with `git rev-parse HEAD` and retain the
  resulting full SHA out of band before review. A commit cannot embed its own
  literal SHA without changing that SHA.

Expected commit chain after the base:

1. `e9a316de16f6c7d4ea1f595417846d30b2041e33` — B01, B02, B04, B06.
2. `ddf282aee94b6af27aeab2e0fc117303ba1a203a` — B03, B05.
3. `593f5805de7a2c71a85fa282a360876532cacf68` — B07.
4. `7aa54c9097dacfa01909d0f5a43c745f27e43a1e` — B08-B11.
5. Commit message `assurance: bind remediation sources and reviewed wave inputs`
   — B12-B14 and this review package.

## Validation facts

- Full QA: 218 passed, 3 host-capability symlink skips.
- Remediation integrity: 33 passed.
- Calibration: 8/8, 1 PASS, 6 FAIL, 1 INCONCLUSIVE, 0 mismatches.
- Overlay: 4/4 source pins and 17/17 mappings/task blobs.
- Main audit comparison: 544 entries, zero differences.
- Ruff, compileall, diff check, review-packet build, anchored adjudication, and
  offline upstream validation: PASS.
- Independent scoped reviews: GO for B01-B14.

## Review paths

- `docs/assurance/reviews/20260713T002206Z_686424795712/remediation/FOUNDATION_REMEDIATION_SUMMARY.md`
- `docs/assurance/reviews/20260713T002206Z_686424795712/remediation/REMEDIATION_STATE.json`
- `docs/assurance/reviews/20260713T002206Z_686424795712/remediation/BLOCKER_MATRIX.csv`
- `docs/assurance/reviews/20260713T002206Z_686424795712/remediation/VALIDATION_REPORT.md`
- `docs/assurance/reviews/20260713T002206Z_686424795712/remediation/RE_REVIEW_INPUT.md`

The reviewer must independently verify exact HEAD equality, commit contents,
external anchors, scope, tests, and protected worktrees before issuing any
reviewed-foundation decision.
