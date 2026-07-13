---
id: JARVIS-0012
title: "Failed archive extraction leaves partial final outputs"
kind: data-integrity
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 1.00
reproducibility: hermetic-always
components: ["backend/src/jarvis_gpt/archive_runtime.py"]
feature_ids: []
requirement_ids: [REQ-ARCHIVE-001, REQ-FILES-001]
scenario_ids: [SCN-LIVE-026]
evidence_ids: [EVID-STATIC-019]
affected_paths: ["backend/src/jarvis_gpt/archive_runtime.py", "backend/src/jarvis_gpt/archive_runtime.py"]
phase_b_scenarios: [SCN-LIVE-026]
candidate_task_ids: [CTASK-0012]
---

# JARVIS-0012 — Failed archive extraction leaves partial final outputs

## 1. Summary

ZIP/TAR/stream extraction writes to final destinations before all limits succeed; 7z validates sizes after extraction.

## 2. Contract and impact

Requirements: REQ-ARCHIVE-001, REQ-FILES-001. A rejected archive can leave a misleading or partially trusted tree that later workflows consume.

## 3. Static evidence

- `backend/src/jarvis_gpt/archive_runtime.py:590-716`
- `backend/src/jarvis_gpt/archive_runtime.py:843-853`

Evidence records: EVID-STATIC-019.

## 4. Hermetic reproduction

See the failing hermetic oracle in EVID-STATIC-019.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: ZIP/TAR/stream extraction writes to final destinations before all limits succeed; 7z validates sizes after extraction.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Extraction has no staging directory/transactional rename and exception cleanup.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `data-integrity` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-026` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Extract into a unique staging root, validate fully, atomically publish, and remove staging on every failure.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0012` and `SCN-LIVE-026`.
