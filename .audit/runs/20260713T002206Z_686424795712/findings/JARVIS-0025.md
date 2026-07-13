---
id: JARVIS-0025
title: "Frontend accessibility and test harness leave interaction regressions unchecked"
kind: test-gap
severity: low
priority: P3
phase_a_status: test-gap
confidence: 0.95
reproducibility: static-proof
components: ["frontend/app/page.tsx"]
feature_ids: []
requirement_ids: [REQ-FRONTEND-001, REQ-TEST-001]
scenario_ids: [SCN-LIVE-031]
evidence_ids: [EVID-STATIC-007, EVID-STATIC-008]
affected_paths: ["frontend/app/page.tsx", "frontend/app/page.tsx", "frontend/package.json", ".github/workflows/ci.yml"]
phase_b_scenarios: [SCN-LIVE-031]
candidate_task_ids: [CTASK-0025]
---

# JARVIS-0025 — Frontend accessibility and test harness leave interaction regressions unchecked

## 1. Summary

Tabs/resize lack complete keyboard/ARIA semantics and there are no frontend unit, component, E2E or accessibility tests.

## 2. Contract and impact

Requirements: REQ-FRONTEND-001, REQ-TEST-001. Keyboard, focus, reduced-motion, stream and stale-state regressions can ship while typecheck/build remain green.

## 3. Static evidence

- `frontend/app/page.tsx:4467-4497`
- `frontend/app/page.tsx:4702-4736`
- `frontend/package.json:5-10`
- `.github/workflows/ci.yml:28-48`

Evidence records: EVID-STATIC-007, EVID-STATIC-008.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Tabs/resize lack complete keyboard/ARIA semantics and there are no frontend unit, component, E2E or accessibility tests.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: The UI is a 5.8k-line monolith and frontend CI has no behavioral test layer.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `test-gap` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-031` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Add component boundaries and browser/component accessibility tests for critical states.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0025` and `SCN-LIVE-031`.
