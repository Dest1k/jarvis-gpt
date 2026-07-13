---
id: JARVIS-0017
title: "Generated document collision logic reuses an existing timestamped path"
kind: data-integrity
severity: medium
priority: P2
phase_a_status: static-confirmed
confidence: 1.00
reproducibility: hermetic-always
components: ["backend/src/jarvis_gpt/document_surfer.py"]
feature_ids: []
requirement_ids: [REQ-FILES-001]
scenario_ids: [SCN-LIVE-027]
evidence_ids: [EVID-STATIC-018]
affected_paths: ["backend/src/jarvis_gpt/document_surfer.py"]
phase_b_scenarios: [SCN-LIVE-027]
candidate_task_ids: [CTASK-0017]
---

# JARVIS-0017 — Generated document collision logic reuses an existing timestamped path

## 1. Summary

After the base path exists, collision fallback has one-second precision and does not verify that the timestamped candidate is unused.

## 2. Contract and impact

Requirements: REQ-FILES-001. Repeated/concurrent generation can overwrite an earlier artifact despite a never-overwrite claim.

## 3. Static evidence

- `backend/src/jarvis_gpt/document_surfer.py:1371-1427`

Evidence records: EVID-STATIC-018.

## 4. Hermetic reproduction

See the failing hermetic oracle in EVID-STATIC-018.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: After the base path exists, collision fallback has one-second precision and does not verify that the timestamped candidate is unused.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: The fallback computes a non-exclusive name instead of atomically reserving a unique path.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `data-integrity` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-027` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Use exclusive create/UUID/counter retry and apply the same rule to archive output directories.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0017` and `SCN-LIVE-027`.
