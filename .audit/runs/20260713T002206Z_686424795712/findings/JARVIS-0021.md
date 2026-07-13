---
id: JARVIS-0021
title: "Main storage lacks versioned migration/integrity/retention and policy corruption fails open"
kind: data-integrity
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.98
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/storage.py"]
feature_ids: []
requirement_ids: [REQ-STORAGE-001, REQ-RESOURCE-001, REQ-SECRET-001]
scenario_ids: [SCN-LIVE-029]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014, EVID-STATIC-016]
affected_paths: ["backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/storage.py", "backend/src/jarvis_gpt/operations.py", "backend/src/jarvis_gpt/operations.py"]
phase_b_scenarios: [SCN-LIVE-029]
candidate_task_ids: [CTASK-0021]
---

# JARVIS-0021 — Main storage lacks versioned migration/integrity/retention and policy corruption fails open

## 1. Summary

Malformed JSON is silently defaulted (browser policy defaults open), the DB has no user_version/migration registry/integrity gate, and append-only audit/events/learning have no purge; conversation delete retains copied dialogue in learning.

## 2. Contract and impact

Requirements: REQ-STORAGE-001, REQ-RESOURCE-001, REQ-SECRET-001. Corruption can weaken policy silently; upgrades/locks are under-specified; state grows without bound and delete semantics are privacy-ambiguous.

## 3. Static evidence

- `backend/src/jarvis_gpt/storage.py:101-107`
- `backend/src/jarvis_gpt/storage.py:386-437`
- `backend/src/jarvis_gpt/storage.py:681-858`
- `backend/src/jarvis_gpt/operations.py:18-25`
- `backend/src/jarvis_gpt/operations.py:67-70`

Evidence records: EVID-STATIC-004, EVID-STATIC-014, EVID-STATIC-016.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Malformed JSON is silently defaulted (browser policy defaults open), the DB has no user_version/migration registry/integrity gate, and append-only audit/events/learning have no purge; conversation delete retains copied dialogue in learning.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Storage evolved through CREATE IF NOT EXISTS and permissive decoders without an explicit lifecycle/retention contract.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `data-integrity` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-029` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Add versioned migrations, strict policy decoding/quarantine, integrity/restore checks and explicit retention/purge semantics.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0021` and `SCN-LIVE-029`.
