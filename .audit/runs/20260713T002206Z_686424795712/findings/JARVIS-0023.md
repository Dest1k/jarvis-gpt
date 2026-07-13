---
id: JARVIS-0023
title: "Unauthenticated health response exposes absolute runtime path"
kind: defensive-design
severity: medium
priority: P2
phase_a_status: static-confirmed
confidence: 0.99
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/api.py"]
feature_ids: []
requirement_ids: [REQ-API-001, REQ-SECRET-001]
scenario_ids: [SCN-LIVE-030]
evidence_ids: [EVID-STATIC-013, EVID-STATIC-021]
affected_paths: ["backend/src/jarvis_gpt/api.py", "backend/src/jarvis_gpt/config.py"]
phase_b_scenarios: [SCN-LIVE-030]
candidate_task_ids: [CTASK-0023]
---

# JARVIS-0023 — Unauthenticated health response exposes absolute runtime path

## 1. Summary

/health bypasses auth and includes settings.home; native defaults bind 0.0.0.0 with no loopback token requirement.

## 2. Contract and impact

Requirements: REQ-API-001, REQ-SECRET-001. A non-loopback deployment discloses host/runtime layout to unauthenticated clients.

## 3. Static evidence

- `backend/src/jarvis_gpt/api.py:507-551`
- `backend/src/jarvis_gpt/config.py:303-305`

Evidence records: EVID-STATIC-013, EVID-STATIC-021.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: /health bypasses auth and includes settings.home; native defaults bind 0.0.0.0 with no loopback token requirement.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Detailed diagnostics and minimal liveness share one public endpoint.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `defensive-design` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-030` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Keep unauthenticated liveness minimal; guard detailed health and fail startup on unsafe bind/token combinations.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0023` and `SCN-LIVE-030`.
