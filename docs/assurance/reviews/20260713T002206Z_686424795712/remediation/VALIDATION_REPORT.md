# Assurance foundation remediation validation

## Final commands

| Check | Result |
| --- | --- |
| `py -3.11 -m pytest qa\tests -q` | PASS: 218 passed, 3 skipped |
| `py -3.11 -m pytest qa\tests\test_remediation_integrity.py -q` | PASS: 33 passed |
| `py -3.11 -m ruff check qa` | PASS |
| `py -3.11 -m compileall qa` | PASS |
| `git diff --check` | PASS |
| `qa.cli validate-suite qa\suites\operator_core` | PASS: 1 scenario |
| `qa.cli validate-evidence` with retained calibration anchor | PASS: 8 records |
| `qa.cli replay` with retained calibration anchor | PASS: 8/8, 0 mismatches |
| `qa.cli build-review-packets` | PASS: 8 packets |
| anchored `qa.cli adjudicate` | PASS: independence and review anchors verified |
| `python -m qa.upstream` on a sanitized internal fixture | PASS |
| `qa.cli validate-overlay-sources` | PASS: 4/4 pins, 17/17 mappings/files |
| external main audit manifest comparison | PASS: 544 entries, 0 differences |

Calibration remained exactly:

```text
8/8 replayed
1 PASS
6 FAIL
1 INCONCLUSIVE
0 mismatches
```

The calibration manifest anchor is
`6019152e13aaffde7f331b0cab2a347025da3ed72e9e92c696dcca773a2dc6d5`.

## Independent regression reviews

- B12 source and mapping binding: GO.
- B13 exact reviewed start and trusted Git boundary: GO.
- B14 complete audit-content manifest: GO.
- Final B12 parser re-review: GO after a 19-case adversarial matrix; the prior
  shadow-structure bypass and adjacent duplicate, scalar, flow, indentation,
  marker, comment, and BOM variants failed closed.

No detailed defensive payload is included in this report.

## External anchors

Only external artifact names and digests are recorded here; absolute backup
paths are intentionally not tracked.

- Candidate bundle SHA-256:
  `f902a5cc215fb9c1fee78d00a1eb1fb0cde3756673c8e702125d1f384cdf6103`.
- Bundle verification: complete history, candidate ref at
  `7d4a4757df9aa3264fd16caf439e40588c375fee`.
- `main-audit-before-final-validation.json`: 544 entries,
  SHA-256 `b9e60ba4ce3988c2ba3209fb354c0ba23fc04100c72b2e02d84097652617b553`.
- `main-audit-after-final-validation.json`: 544 entries,
  SHA-256 `b9e60ba4ce3988c2ba3209fb354c0ba23fc04100c72b2e02d84097652617b553`.
- `main-audit-final-comparison.json`: zero differences,
  SHA-256 `79423531c085d229732a1c85298a25a4dbdba69744cd6a2c1558368c5dc188d7`.

## Protected state

- Main and `origin/main`: `b2c481de1a9e68079a67ff49790eb685a09e80e5`.
- Main tracked/index status: clean.
- Main untracked path baseline: 269 before and after, zero path differences.
- Candidate and review worktrees: exact
  `7d4a4757df9aa3264fd16caf439e40588c375fee`, clean status.
- Annotated pre-remediation tag resolves to the exact candidate commit.
- `.audit/**`, production source, runtime state, dependencies, and external
  network were not changed or used.
- No push, merge, product readiness marker, or self-attestation occurred.
