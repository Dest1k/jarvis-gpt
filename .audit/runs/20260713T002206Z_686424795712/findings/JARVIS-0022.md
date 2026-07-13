---
id: JARVIS-0022
title: "Web-watch persists digest before durable notification delivery"
kind: data-integrity
severity: medium
priority: P2
phase_a_status: static-confirmed
confidence: 0.98
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/autonomy_executor.py"]
feature_ids: []
requirement_ids: [REQ-JOBS-001, REQ-STORAGE-001]
scenario_ids: [SCN-LIVE-021]
evidence_ids: [EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/autonomy_executor.py"]
phase_b_scenarios: [SCN-LIVE-021]
candidate_task_ids: [CTASK-0022]
---

# JARVIS-0022 — Web-watch persists digest before durable notification delivery

## 1. Summary

The new digest is committed before event, memory and bus notification; a crash/error afterward makes retry see no change.

## 2. Contract and impact

Requirements: REQ-JOBS-001, REQ-STORAGE-001. A page change alert can be lost permanently with no reconciliation evidence.

## 3. Static evidence

- `backend/src/jarvis_gpt/autonomy_executor.py:378-416`

Evidence records: EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: The new digest is committed before event, memory and bus notification; a crash/error afterward makes retry see no change.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Observed state and notification delivery are not connected by an outbox/acknowledgement state machine.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `data-integrity` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-021` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Persist detection plus pending notification atomically and acknowledge only after durable delivery.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0022` and `SCN-LIVE-021`.
