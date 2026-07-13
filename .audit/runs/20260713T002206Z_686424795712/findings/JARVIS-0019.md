---
id: JARVIS-0019
title: "Mutating document/watch tools default to safe and bypass approval"
kind: spec-gap
severity: high
priority: P1
phase_a_status: spec-gap
confidence: 0.97
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/tools.py"]
feature_ids: []
requirement_ids: [REQ-APPROVAL-001, REQ-FILES-001]
scenario_ids: [SCN-LIVE-028]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/tools.py", "backend/src/jarvis_gpt/tools.py", "backend/src/jarvis_gpt/tools.py", "backend/src/jarvis_gpt/tools.py"]
phase_b_scenarios: [SCN-LIVE-028]
candidate_task_ids: [CTASK-0019]
---

# JARVIS-0019 — Mutating document/watch tools default to safe and bypass approval

## 1. Summary

Document mutation/archive extraction/create and recurring web.watch add/remove omit danger_level, inheriting safe and skipping registry approval.

## 2. Contract and impact

Requirements: REQ-APPROVAL-001, REQ-FILES-001. Durable external jobs and filesystem outputs can be created through a lower trust gate than file.write.

## 3. Static evidence

- `backend/src/jarvis_gpt/tools.py:319-327`
- `backend/src/jarvis_gpt/tools.py:1405-1626`
- `backend/src/jarvis_gpt/tools.py:1806-1842`
- `backend/src/jarvis_gpt/tools.py:548-649`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Document mutation/archive extraction/create and recurring web.watch add/remove omit danger_level, inheriting safe and skipping registry approval.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: No explicit product contract classifies these side effects; default-safe masks omissions.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `spec-gap` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-028` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Define danger policy per action and make missing classification fail closed for mutating tools.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0019` and `SCN-LIVE-028`.
