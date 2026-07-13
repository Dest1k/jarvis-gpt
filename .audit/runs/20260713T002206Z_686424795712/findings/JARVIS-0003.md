---
id: JARVIS-0003
title: "Launcher process cleanup signature can match unrelated processes"
kind: reliability
severity: high
priority: P1
phase_a_status: probable-runtime
confidence: 0.91
reproducibility: runtime-required
components: ["scripts/jarvis-launcher.ps1"]
feature_ids: []
requirement_ids: [REQ-LAUNCHER-001]
scenario_ids: [SCN-LIVE-004]
evidence_ids: [EVID-STATIC-009]
affected_paths: ["scripts/jarvis-launcher.ps1", "scripts/jarvis-launcher.ps1", "scripts/jarvis-launcher.ps1"]
phase_b_scenarios: [SCN-LIVE-004]
candidate_task_ids: [CTASK-0003]
---

# JARVIS-0003 — Launcher process cleanup signature can match unrelated processes

## 1. Summary

Any process whose executable or command line contains the repo/frontend path is treated as a Jarvis process and terminated.

## 2. Contract and impact

Requirements: REQ-LAUNCHER-001. Editors, terminals or diagnostic commands mentioning the repo path may be killed.

## 3. Static evidence

- `scripts/jarvis-launcher.ps1:894-910`
- `scripts/jarvis-launcher.ps1:1066-1083`
- `scripts/jarvis-launcher.ps1:1674`

Evidence records: EVID-STATIC-009.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Any process whose executable or command line contains the repo/frontend path is treated as a Jarvis process and terminated.

## 6. Runtime confirmation

Required before converting probability into a runtime-confirmed defect.

## 7. Root cause

Confirmed design/code cause: Cleanup uses substring ownership inference instead of recorded PID plus birth identity.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `reliability` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-004` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Track exact child identities and terminate only verified descendants owned by the active launcher state.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0003` and `SCN-LIVE-004`.
