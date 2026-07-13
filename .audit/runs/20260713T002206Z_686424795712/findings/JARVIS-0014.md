---
id: JARVIS-0014
title: "Command Center advertises a live WebSocket feed whose transport is disabled"
kind: spec-gap
severity: medium
priority: P2
phase_a_status: spec-gap
confidence: 0.99
reproducibility: static-proof
components: ["frontend/app/page.tsx"]
feature_ids: []
requirement_ids: [REQ-STREAM-001, REQ-LEGACY-001, REQ-DOC-001]
scenario_ids: [SCN-LIVE-013]
evidence_ids: [EVID-STATIC-007, EVID-STATIC-008]
affected_paths: ["frontend/app/page.tsx", "frontend/app/page.tsx", "frontend/app/page.tsx", "README.md", "docs/assistant-notes.md"]
phase_b_scenarios: [SCN-LIVE-013]
candidate_task_ids: [CTASK-0014]
---

# JARVIS-0014 — Command Center advertises a live WebSocket feed whose transport is disabled

## 1. Summary

wsUrl always returns empty and the connection effect exits, yet active UI shows waiting for events and public docs promise /ws/events.

## 2. Contract and impact

Requirements: REQ-STREAM-001, REQ-LEGACY-001, REQ-DOC-001. Operators cannot distinguish disabled realtime transport from an idle healthy feed.

## 3. Static evidence

- `frontend/app/page.tsx:880-885`
- `frontend/app/page.tsx:1950-1955`
- `frontend/app/page.tsx:3940-3955`
- `README.md:26`
- `docs/assistant-notes.md:1266-1269`

Evidence records: EVID-STATIC-007, EVID-STATIC-008.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: wsUrl always returns empty and the connection effect exits, yet active UI shows waiting for events and public docs promise /ws/events.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Direct browser WS was removed without a same-origin replacement or UX/docs contract update.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `spec-gap` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-013` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Implement authenticated same-origin realtime transport or remove/label the inactive surface consistently.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0014` and `SCN-LIVE-013`.
