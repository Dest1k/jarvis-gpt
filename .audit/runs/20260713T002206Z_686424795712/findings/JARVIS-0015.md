---
id: JARVIS-0015
title: "Frontend retains stale online/ready state after polling failures"
kind: ux
severity: medium
priority: P2
phase_a_status: probable-runtime
confidence: 0.96
reproducibility: runtime-required
components: ["frontend/app/page.tsx"]
feature_ids: []
requirement_ids: [REQ-FRONTEND-001]
scenario_ids: [SCN-LIVE-012]
evidence_ids: [EVID-STATIC-007, EVID-STATIC-008]
affected_paths: ["frontend/app/page.tsx", "frontend/app/page.tsx", "frontend/app/page.tsx"]
phase_b_scenarios: [SCN-LIVE-012]
candidate_task_ids: [CTASK-0015]
---

# JARVIS-0015 — Frontend retains stale online/ready state after polling failures

## 1. Summary

Promise.allSettled rejections are never inspected and previous snapshots remain indefinitely; online is derived from the existence of old status.

## 2. Contract and impact

Requirements: REQ-FRONTEND-001. A stopped backend/dispatcher can remain displayed as online/ready with no stale timestamp.

## 3. Static evidence

- `frontend/app/page.tsx:1896-1926`
- `frontend/app/page.tsx:2136-2163`
- `frontend/app/page.tsx:3960-3962`

Evidence records: EVID-STATIC-007, EVID-STATIC-008.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Promise.allSettled rejections are never inspected and previous snapshots remain indefinitely; online is derived from the existence of old status.

## 6. Runtime confirmation

Required before converting probability into a runtime-confirmed defect.

## 7. Root cause

Confirmed design/code cause: Polling stores values but no freshness/error state machine.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `ux` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-012` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Track last-success/failure per source, expire snapshots and render explicit stale/offline/degraded states.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0015` and `SCN-LIVE-012`.
