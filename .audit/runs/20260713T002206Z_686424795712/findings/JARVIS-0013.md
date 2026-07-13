---
id: JARVIS-0013
title: "User regex can block the async event loop beyond cancellation budgets"
kind: performance
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.98
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/tools.py"]
feature_ids: []
requirement_ids: [REQ-RESOURCE-001, REQ-JOBS-001]
scenario_ids: [SCN-LIVE-022]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/tools.py", "backend/src/jarvis_gpt/autonomy_executor.py", "backend/src/jarvis_gpt/document_surfer.py", "backend/src/jarvis_gpt/document_surfer.py"]
phase_b_scenarios: [SCN-LIVE-022]
candidate_task_ids: [CTASK-0013]
---

# JARVIS-0013 — User regex can block the async event loop beyond cancellation budgets

## 1. Summary

web.watch and document/archive searches run arbitrary syntactically valid Python regex synchronously; asyncio timeouts cannot preempt catastrophic matching.

## 2. Contract and impact

Requirements: REQ-RESOURCE-001, REQ-JOBS-001. A small input can stall chat, jobs, health and cancellation for an unbounded interval.

## 3. Static evidence

- `backend/src/jarvis_gpt/tools.py:8601-8650`
- `backend/src/jarvis_gpt/autonomy_executor.py:311-350`
- `backend/src/jarvis_gpt/document_surfer.py:500-560`
- `backend/src/jarvis_gpt/document_surfer.py:756-805`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: web.watch and document/archive searches run arbitrary syntactically valid Python regex synchronously; asyncio timeouts cannot preempt catastrophic matching.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Regex complexity is not constrained and execution is not isolated in a killable worker.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `performance` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-022` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Use a safe regex engine/subset or isolated subprocess with hard wall-clock and size budgets.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0013` and `SCN-LIVE-022`.
