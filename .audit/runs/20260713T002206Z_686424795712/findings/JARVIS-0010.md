---
id: JARVIS-0010
title: "Transport retries can repeat actions after replay retention or new chat authorization"
kind: data-integrity
severity: high
priority: P1
phase_a_status: probable-runtime
confidence: 0.97
reproducibility: runtime-required
components: ["backend/src/jarvis_gpt/models.py"]
feature_ids: []
requirement_ids: [REQ-IDEMPOTENCY-001, REQ-STREAM-001]
scenario_ids: [SCN-LIVE-016]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/models.py", "backend/src/jarvis_gpt/agent.py", "backend/src/jarvis_gpt/execution_replay.py", "backend/src/jarvis_gpt/execution_kernel.py", "backend/src/jarvis_gpt/tools.py"]
phase_b_scenarios: [SCN-LIVE-016]
candidate_task_ids: [CTASK-0010]
---

# JARVIS-0010 — Transport retries can repeat actions after replay retention or new chat authorization

## 1. Summary

Chat has no request idempotency or per-conversation serialization; old durable execution keys are evicted with no tombstone.

## 2. Contract and impact

Requirements: REQ-IDEMPOTENCY-001, REQ-STREAM-001. A lost response/retry can duplicate append/process/GUI effects; concurrent turns can interleave; a sufficiently old execution key can be reapplied.

## 3. Static evidence

- `backend/src/jarvis_gpt/models.py:23-30`
- `backend/src/jarvis_gpt/agent.py:679-941`
- `backend/src/jarvis_gpt/execution_replay.py:88-100`
- `backend/src/jarvis_gpt/execution_kernel.py:501-542`
- `backend/src/jarvis_gpt/tools.py:9598-9659`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Chat has no request idempotency or per-conversation serialization; old durable execution keys are evicted with no tombstone.

## 6. Runtime confirmation

Required before converting probability into a runtime-confirmed defect.

## 7. Root cause

Confirmed design/code cause: Idempotency is scoped to short-lived authorization/retained results rather than a caller key with an explicit durable expiry contract.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `data-integrity` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-016` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Require request IDs, serialize conversation turns and retain non-replay tombstones beyond result-detail retention.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0010` and `SCN-LIVE-016`.
