---
id: JARVIS-0008
title: "Autonomy job JSON-array RMW loses concurrency and detached start reports false success"
kind: reliability
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.99
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/operations.py"]
feature_ids: []
requirement_ids: [REQ-JOBS-001, REQ-IDEMPOTENCY-001]
scenario_ids: [SCN-LIVE-019]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/operations.py", "backend/src/jarvis_gpt/api.py", "backend/src/jarvis_gpt/autonomy_executor.py"]
phase_b_scenarios: [SCN-LIVE-019]
candidate_task_ids: [CTASK-0008]
---

# JARVIS-0008 — Autonomy job JSON-array RMW loses concurrency and detached start reports false success

## 1. Summary

Jobs/history are whole JSON arrays updated without CAS; /start emits started before admission, while paused/done/cancelled/already-running jobs are rejected silently by the executor.

## 2. Contract and impact

Requirements: REQ-JOBS-001, REQ-IDEMPOTENCY-001. Concurrent updates can be lost and observers can see a started job that never ran or produced a terminal event.

## 3. Static evidence

- `backend/src/jarvis_gpt/operations.py:238-393`
- `backend/src/jarvis_gpt/api.py:1083-1094`
- `backend/src/jarvis_gpt/autonomy_executor.py:73-109`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Jobs/history are whole JSON arrays updated without CAS; /start emits started before admission, while paused/done/cancelled/already-running jobs are rejected silently by the executor.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Durable job state is not row/lease based and transport success is emitted before authoritative admission.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `reliability` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-019` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Use transactional row/CAS state transitions and return/publish the result of a single admission decision.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0008` and `SCN-LIVE-019`.
