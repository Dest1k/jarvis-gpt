---
id: JARVIS-0009
title: "Approval state/audit are non-atomic and raw payloads can retain credentials"
kind: data-integrity
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.99
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/storage.py"]
feature_ids: []
requirement_ids: [REQ-APPROVAL-001, REQ-SECRET-001, REQ-STORAGE-001]
scenario_ids: [SCN-LIVE-017]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/storage.py"]
phase_b_scenarios: [SCN-LIVE-017]
candidate_task_ids: [CTASK-0009]
---

# JARVIS-0009 — Approval state/audit are non-atomic and raw payloads can retain credentials

## 1. Summary

Approval transitions commit separately from audit writes, and creation persists exact unredacted tool arguments in both approval and audit rows.

## 2. Contract and impact

Requirements: REQ-APPROVAL-001, REQ-SECRET-001, REQ-STORAGE-001. Audit failure can strand/ambiguously report approval state; nested environment/argument credentials can persist and be returned by API.

## 3. Static evidence

- `backend/src/jarvis_gpt/storage.py:1722-1765`
- `backend/src/jarvis_gpt/storage.py:2240-2292`
- `backend/src/jarvis_gpt/storage.py:2403-2435`
- `backend/src/jarvis_gpt/storage.py:2558-2589`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Approval transitions commit separately from audit writes, and creation persists exact unredacted tool arguments in both approval and audit rows.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Approval persistence lacks a transactional outbox and redaction is applied to terminal results but not creation/audit payloads.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `data-integrity` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-017` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Redact before persistence and commit transition plus durable event/outbox atomically.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0009` and `SCN-LIVE-017`.
