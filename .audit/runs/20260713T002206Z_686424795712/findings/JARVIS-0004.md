---
id: JARVIS-0004
title: "Compose frontend waits for a backend healthcheck that does not exist"
kind: defect
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.99
reproducibility: static-proof
components: ["docker-compose.yml"]
feature_ids: []
requirement_ids: [REQ-LAUNCHER-001, REQ-OFFLINE-001]
scenario_ids: [SCN-LIVE-007]
evidence_ids: [EVID-STATIC-013, EVID-STATIC-021]
affected_paths: ["docker-compose.yml", "docker-compose.yml"]
phase_b_scenarios: [SCN-LIVE-007]
candidate_task_ids: [CTASK-0004]
---

# JARVIS-0004 — Compose frontend waits for a backend healthcheck that does not exist

## 1. Summary

frontend depends_on backend with condition service_healthy, but backend defines no healthcheck.

## 2. Contract and impact

Requirements: REQ-LAUNCHER-001, REQ-OFFLINE-001. Compose startup can reject the dependency or leave frontend blocked, depending on Compose implementation/version.

## 3. Static evidence

- `docker-compose.yml:124-145`
- `docker-compose.yml:43-123`

Evidence records: EVID-STATIC-013, EVID-STATIC-021.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: frontend depends_on backend with condition service_healthy, but backend defines no healthcheck.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Dependency condition and service contract drifted independently.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `defect` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-007` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Add a pinned backend healthcheck or use a condition whose semantics match the service definition, then test rendered Compose in CI.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0004` and `SCN-LIVE-007`.
