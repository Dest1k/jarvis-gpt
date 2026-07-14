# Validation evidence index — independent Grok re-review

## Identity

- reviewer_provider: xAI
- reviewer_model: Grok 4.5
- independence_level: DIFFERENT_PROVIDER
- review_run_id: assurance-rereview-grok-20260713T002206Z_686424795712-fd1a548e9362c0b7
- context_id: rereview-grok-fd1a548e9362c0b7
- run_nonce: fd1a548e9362c0b7

## Candidate

- remediated foundation: `b4ae74f740b4751e30941102fafe0455f885760d`
- original foundation: `7d4a4757df9aa3264fd16caf439e40588c375fee`
- main/origin-main: `b2c481de1a9e68079a67ff49790eb685a09e80e5`

## Exact five remediation commits

1. `e9a316de16f6c7d4ea1f595417846d30b2041e33` — B01, B02, B04, B06
2. `ddf282aee94b6af27aeab2e0fc117303ba1a203a` — B03, B05
3. `593f5805de7a2c71a85fa282a360876532cacf68` — B07
4. `7aa54c9097dacfa01909d0f5a43c745f27e43a1e` — B08–B11
5. `b4ae74f740b4751e30941102fafe0455f885760d` — B12–B14

## Local validation commands and counts

| Check | Result |
| --- | --- |
| `py -3.11 -m pytest qa\tests -q` | 218 passed, 3 skipped |
| `py -3.11 -m pytest qa\tests\test_remediation_integrity.py -q` | 33 passed |
| matrix-named B01–B14 regressions | 86 passed |
| `py -3.11 -m pytest qa\tests\test_upstream.py -q` | 59 passed, 1 skipped |
| `py -3.11 -m ruff check qa` | PASS |
| `py -3.11 -m compileall qa` | PASS |
| `git diff --check` base..candidate | PASS |
| `qa.cli validate-suite qa\suites\operator_core` | ok, 1 scenario |
| `qa.cli validate-evidence` + retained anchor | ok, 8 records |
| `qa.cli replay` + retained anchor | 8/8, 1 PASS, 6 FAIL, 1 INCONCLUSIVE, 0 mismatches |
| `qa.cli build-review-packets` | 8 packets |
| `qa.cli validate-overlay-sources` | 4/4 pins, 17/17 mappings |
| independent Git ODB pin rehash | 4/4 MATCH |
| independent adversarial probes | 16/16 PASS |
| main untracked baseline | 269 paths, 0 diffs vs bootstrap before manifest |
| main audit external manifests | 544/544, identical SHA-256, 0 differences |

Calibration anchor (manifest SHA-256):
`6019152e13aaffde7f331b0cab2a347025da3ed72e9e92c696dcca773a2dc6d5`

## External review backup (not Git-tracked)

Root: `D:\jarvis\audit-backups\20260713T002206Z_686424795712\assurance-rereview-grok`

Notable artifacts:

- `candidate-b4ae74f.bundle` (+ sha256 manifest)
- `candidate-tree.zip`
- `REVIEW_IDENTITY.txt`
- `evidence\pytest-full.txt`
- `evidence\pytest-integrity.txt`
- `evidence\matrix-named.txt`
- `evidence\cli-*.txt`
- `evidence\independent-probes-final.json`
- `probes\**` (temporary adversarial only)

## Scope

- 55 changed paths between original and remediated candidates
- 0 forbidden production/runtime/`.audit` tracked changes
- 0 out-of-scope paths

## Cross-cutting fail-closed

Reproduced or matrix-covered: forged evidence cannot validate under retained anchor; wrong anchor fails; deterministic FAIL calibration remains FAIL under replay; packet build requires verified replay path; path escapes denied; canaries redacted; noncanonical URLs rejected; exact HEAD required (ancestor insufficient); audit content mutation detected; overlay pin/mapping substitution covered by integrity suite.

## Residual risks

- Three host-capability real-symlink skips on Windows; simulated reparse coverage and independent reparse-boundary checks passed.
- Hash binding is integrity evidence, not cryptographic authentication of authors.
- License validator does not issue legal approval.
- Wave execution still requires a clean ignored-inclusive worktree (`__pycache__` fails closed).

## Non-actions

No production/runtime/Docker/JARVIS/dispatcher/model start. No dependency install. No external network. No push/merge. No Wave 0. Remediation branch and worktree not modified by this review.
