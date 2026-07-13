---
id: JARVIS-0001
title: "Model activation accepts unverified directories and has no rollback"
kind: reliability
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.99
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/model_hub.py"]
feature_ids: []
requirement_ids: [REQ-PROFILE-001, REQ-IDEMPOTENCY-001]
scenario_ids: [SCN-LIVE-011]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-007]
affected_paths: ["backend/src/jarvis_gpt/model_hub.py", "backend/src/jarvis_gpt/model_catalog.py", "backend/src/jarvis_gpt/api.py", "frontend/app/page.tsx"]
phase_b_scenarios: [SCN-LIVE-011]
candidate_task_ids: [CTASK-0001]
---

# JARVIS-0001 — Model activation accepts unverified directories and has no rollback

## 1. Summary

An empty custom model directory is activatable; the UI persists the override, stops the working dispatcher and starts the replacement without checking either result or restoring the prior runtime.

## 2. Contract and impact

Requirements: REQ-PROFILE-001, REQ-IDEMPOTENCY-001. A failed switch can leave a durable invalid override and stop the previously working model while API/UI transport still looks successful.

## 3. Static evidence

- `backend/src/jarvis_gpt/model_hub.py:272-300`
- `backend/src/jarvis_gpt/model_catalog.py:138-144`
- `backend/src/jarvis_gpt/api.py:869-890`
- `frontend/app/page.tsx:3174-3214`

Evidence records: EVID-STATIC-004, EVID-STATIC-007.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: An empty custom model directory is activatable; the UI persists the override, stops the working dispatcher and starts the replacement without checking either result or restoring the prior runtime.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Activation validates identity/name rather than artifacts and is implemented as several non-transactional state changes.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `reliability` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-011` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Introduce a manifest/shard/architecture compatibility gate and a staged switch with health confirmation and rollback.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0001` and `SCN-LIVE-011`.
