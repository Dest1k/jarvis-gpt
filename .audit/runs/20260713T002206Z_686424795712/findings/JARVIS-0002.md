---
id: JARVIS-0002
title: "Launcher stop fails open when ownership state is missing or corrupt"
kind: reliability
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.98
reproducibility: static-proof
components: ["scripts/jarvis-launcher.ps1"]
feature_ids: []
requirement_ids: [REQ-LAUNCHER-001]
scenario_ids: [SCN-LIVE-003]
evidence_ids: [EVID-STATIC-009]
affected_paths: ["scripts/jarvis-launcher.ps1", "scripts/jarvis-launcher.ps1"]
phase_b_scenarios: [SCN-LIVE-003]
candidate_task_ids: [CTASK-0002]
---

# JARVIS-0002 — Launcher stop fails open when ownership state is missing or corrupt

## 1. Summary

Missing/corrupt launcher-state.json is interpreted as ownership of the dispatcher instead of unknown ownership.

## 2. Contract and impact

Requirements: REQ-LAUNCHER-001. A stop invocation can terminate a dispatcher not started by this launcher run.

## 3. Static evidence

- `scripts/jarvis-launcher.ps1:1245-1288`
- `scripts/jarvis-launcher.ps1:1680-1684`

Evidence records: EVID-STATIC-009.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Missing/corrupt launcher-state.json is interpreted as ownership of the dispatcher instead of unknown ownership.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Ownership predicate defaults to true on absent evidence and state is written non-atomically.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `reliability` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-003` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Fail closed on absent/corrupt state; persist state atomically with runtime identity and verify it before stop.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0002` and `SCN-LIVE-003`.
