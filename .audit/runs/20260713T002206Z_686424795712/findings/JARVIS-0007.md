---
id: JARVIS-0007
title: "JarvisStorage operations can leave a poisoned transaction after exceptions"
kind: data-integrity
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.99
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/storage.py"]
feature_ids: []
requirement_ids: [REQ-STORAGE-001]
scenario_ids: [SCN-LIVE-018]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/storage.py"]
phase_b_scenarios: [SCN-LIVE-018]
candidate_task_ids: [CTASK-0007]
---

# JARVIS-0007 — JarvisStorage operations can leave a poisoned transaction after exceptions

## 1. Summary

Several DB+filesystem methods commit only after MemoryVault/file work and do not rollback when that later work throws.

## 2. Contract and impact

Requirements: REQ-STORAGE-001. A failed call can be committed by a later unrelated operation; DB and vault may diverge.

## 3. Static evidence

- `backend/src/jarvis_gpt/storage.py:940-998`
- `backend/src/jarvis_gpt/storage.py:1136-1194`
- `backend/src/jarvis_gpt/storage.py:1857-1932`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Several DB+filesystem methods commit only after MemoryVault/file work and do not rollback when that later work throws.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: One long-lived connection is managed by ad-hoc commits rather than an exception-safe unit-of-work boundary.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `data-integrity` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-018` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Wrap each logical mutation in explicit transaction/rollback and define ordering or an outbox for filesystem mirrors.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0007` and `SCN-LIVE-018`.
