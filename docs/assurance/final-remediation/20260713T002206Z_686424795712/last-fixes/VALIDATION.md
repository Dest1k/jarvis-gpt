# VALIDATION — final release last-fixes

## 1. Pinned Ruff 0.8.4

- `py -3.11 -m ruff --version` → **0.8.4**
- `py -3.11 -m ruff check --no-cache backend/src backend/tests` → **All checks passed, exit 0**

## 2. Exact CI lint

- Same scopes as `.github/workflows/ci.yml` Lint backend → **PASS**

## 3. Real clean `jarvis.cmd doctor` / `scripts/doctor.ps1`

- Evidence: `D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-release-last-fixes\doctor-clean2.json`
- **Exit 0**, `ok=true`, failed=0, passed=10, degraded=false
- `timeouts.backend_tests_seconds=600`
- Checks: backend tests/lint/compile, docker compose config, frontend audit/typecheck/build, backend health/autonomy, frontend HTTP — all passed when stack was up
- Elapsed ~218 s (suite no longer killed at 180 s)

## 4. Forced required doctor failure

- `JARVIS_DOCTOR_TEST_TIMEOUT_SECONDS=0` → exit **2**, clear range error, `ok=false`
- Simulated required backend-tests failure → exit **1**, `ok=false`
- Unit regressions in `test_smoke_script.py` PASS

## 5. Full backend suite

- `pytest -q`: **877 passed, 13 skipped**, ~186 s, exit 0

## 6. QA suite and calibration

- `pytest qa`: **218 passed, 3 skipped**
- `qa.cli validate-suite qa/suites/operator_core` → ok, 1 scenario
- `qa.cli validate-evidence qa/tests/fixtures/calibration_evidence.jsonl` → ok, 8 records
- `qa.cli replay` same fixture → ok, 8 cases (1 PASS / 6 FAIL / 1 INCONCLUSIVE), 0 mismatches

## 7. Frontend typecheck/build

- `npm run typecheck` exit 0
- `npm run build` exit 0 (Next.js 16.2.10)

## 8–10. Live exact artifact / clarify / follow-up

Runtime home: `...\final-release-last-fixes\runtime-home` (temporary allowed root under audit-backups).

| Gate | Result |
|------|--------|
| Exact artifact 3/3 | **PASS** — `exact-live-1.md`..`3.md` exist with markers |
| Zero document-search misroute | **PASS** |
| Zero timestamp fallback | **PASS** |
| Zero missing claimed artifact | **PASS** |
| Ambiguous clarification 6/6 | **PASS** |
| Follow-up artifact 3/3 | **PASS** — `report-follow-live1..3.md` |

Evidence JSON: `...\final-release-last-fixes\live-results\`

## 11–12. Existing-document recall / transform

- Intent classify: EXISTING vs NEW vs TRANSFORM → PASS
- Transform `documents.generate` with source under runtime home → exact `transform-out-live.md`, source hash unchanged
- Live upload HTTP 405 on this stack (non-blocking); classification + offline transform covered

## 13. Token redaction

- Doctor run with canary `CANARY_DOCTOR_LASTFIX_deadbeefcafe` → absent from stdout/stderr/JSON

## 14. Internal-output integrity

- Targeted agent protocol/stream tests: PASS
- Full suite green includes envelope guards

## 15. Runtime/session isolation

- Isolation-related tests: 29 passed
- Live runtime home is isolated under audit-backups last-fixes path

## 16. Secret scan

- Diff base..HEAD: **0** credential-like hits

## 17. `git diff --check`

- Clean after fixing 4 blank-line-at-EOF warnings in prior remediation docs only

## 18. Cleanup / baseline

- No push/merge
- Main not modified
- Other review worktrees not used for edits
- Candidate status only: `FINAL_RELEASE_LAST_FIXES_CANDIDATE_FOR_REVIEW`
