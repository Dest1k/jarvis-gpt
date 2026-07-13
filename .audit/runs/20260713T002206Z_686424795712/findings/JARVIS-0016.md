---
id: JARVIS-0016
title: "Frontend stream accepts EOF without terminal state and cannot cancel requests"
kind: ux
severity: medium
priority: P2
phase_a_status: probable-runtime
confidence: 0.90
reproducibility: runtime-required
components: ["frontend/app/page.tsx"]
feature_ids: []
requirement_ids: [REQ-STREAM-001, REQ-IDEMPOTENCY-001]
scenario_ids: [SCN-LIVE-014]
evidence_ids: [EVID-STATIC-007, EVID-STATIC-008]
affected_paths: ["frontend/app/page.tsx", "frontend/app/page.tsx"]
phase_b_scenarios: [SCN-LIVE-014]
candidate_task_ids: [CTASK-0016]
---

# JARVIS-0016 — Frontend stream accepts EOF without terminal state and cannot cancel requests

## 1. Summary

Normal EOF is treated as success even without done/error, leaving a pending bubble; no AbortController exists for cancel/window switch.

## 2. Contract and impact

Requirements: REQ-STREAM-001, REQ-IDEMPOTENCY-001. Interrupted turns can appear stuck or successful and late updates can reach the wrong UI state.

## 3. Static evidence

- `frontend/app/page.tsx:844-874`
- `frontend/app/page.tsx:2367-2480`

Evidence records: EVID-STATIC-007, EVID-STATIC-008.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Normal EOF is treated as success even without done/error, leaving a pending bubble; no AbortController exists for cancel/window switch.

## 6. Runtime confirmation

Required before converting probability into a runtime-confirmed defect.

## 7. Root cause

Confirmed design/code cause: Transport parser lacks a required terminal-state contract and request lifecycle ownership.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `ux` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-014` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Require exactly one terminal event, persist interrupted state and bind an AbortController to each turn/window.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0016` and `SCN-LIVE-014`.
