# VALIDATION — release blocker remediation

## 1. Pinned Ruff

- Command: `py -3.11 -m ruff check --no-cache backend/src backend/tests`
- Version: ruff 0.8.4
- Result: **All checks passed! exit 0**

## 2. CI-equivalent backend lint

- `python -m ruff check backend/src backend/tests` (same scopes as `.github/workflows/ci.yml`)
- Result: **PASS**

## 3–4. Doctor / smoke lint contract

- `scripts/smoke.py` backend lint uses the same `ruff check backend/src backend/tests`.
- Regression: `test_doctor_and_ci_share_pinned_ruff_lint_contract` PASS.
- Clean doctor full run on this host still subject to P2 suite-timeout (180s bound vs ~300s suite) — not re-introduced by RB-1.
- Forced required failure: smoke/doctor nonzero contract retained (SPARK-0016 tests).

## 5. Full backend suite

- `pytest backend/tests`: **862 passed, 13 skipped**

## 6. QA suite/calibration

- `pytest qa/tests`: **218 passed, 3 skipped**
- `validate-suite operator_core`: scenario present under `qa/suites/operator_core`

## 7. Frontend

- `npm install` then `npx tsc --noEmit` PASS; `npx next build` PASS (Next.js 16.2.10)

## 8–9. Live clarification / follow-up

- Isolated home: `D:\jarvis-rb-home-20260713`
- Prompt (Claude): `prepare the report file in the right format and put it where it belongs`
- Live API **6/6**: one `?` / Уточните, **0** document-outputs files, **0** missions (recorded while turbo backend was healthy).
- Follow-up: after answer `md, exact name report.md, directory document-outputs, content: ...` deterministic resume creates `report.md` under document-outputs (offline + code path); shopping hijack fixed.
- Evidence: `D:\tmp\rb-live\clarify-6x.json` (6/6), offline follow debug, `docs/.../release-blocker-remediation/`.

## 10. Quick non-regression

Covered by existing suites retained green: redaction/smoke, internal output integrity tests, artifact path tests, multi-turn refs, runtime isolation tests from prior candidate (not re-failed by this narrow fix).

## 11. Secret scan

- Diff `8aa2823ce40a8ed41555a8b1f9ec89de59deaad3..HEAD`: no API keys/tokens/private keys in added lines.

## 12. Cleanup / baseline

- Old final-remediation worktree remains at `8aa2823ce40a8ed41555a8b1f9ec89de59deaad3`.
- No push/merge.
- Temporary Admin-home accidental start cleaned (jarvis paths only).

### Follow-up evidence detail

- Live API: clarify **6/6**; follow created artifacts (2 distinct new files in one run; third was same-path overwrite).
- Deterministic resume offline with FailLLM: **3/3** unique `report-followN.md` files, zero missions.
