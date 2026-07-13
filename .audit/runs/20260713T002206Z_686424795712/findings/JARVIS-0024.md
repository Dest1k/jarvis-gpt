---
id: JARVIS-0024
title: "Bundled web synthesis/TLS trust policy is weaker than the core path"
kind: defensive-design
severity: medium
priority: P2
phase_a_status: spec-gap
confidence: 0.95
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/agent.py"]
feature_ids: []
requirement_ids: [REQ-TRUST-001, REQ-URL-001, REQ-DOC-001]
scenario_ids: [SCN-LIVE-024]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/agent.py", "backend/src/jarvis_gpt/agent.py", "backend/src/jarvis_gpt/agent.py", "backend/src/jarvis_gpt/web_surfer.py"]
phase_b_scenarios: [SCN-LIVE-024]
candidate_task_ids: [CTASK-0024]
---

# JARVIS-0024 — Bundled web synthesis/TLS trust policy is weaker than the core path

## 1. Summary

The dedicated web synthesis prompt omits the core never-follow-source-instructions rule and surfer contexts ignore TLS certificate errors.

## 2. Contract and impact

Requirements: REQ-TRUST-001, REQ-URL-001, REQ-DOC-001. Untrusted page instructions/provenance and invalid TLS are handled inconsistently across web paths.

## 3. Static evidence

- `backend/src/jarvis_gpt/agent.py:221-230`
- `backend/src/jarvis_gpt/agent.py:3600-3620`
- `backend/src/jarvis_gpt/agent.py:3812-3845`
- `backend/src/jarvis_gpt/web_surfer.py:536-550`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: The dedicated web synthesis prompt omits the core never-follow-source-instructions rule and surfer contexts ignore TLS certificate errors.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: The bundled surfer/synthesis path bypasses shared trust metadata and transport policy.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `defensive-design` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-024` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Reuse one untrusted-content prompt/provenance contract and fail closed on TLS unless a narrowly approved exception exists.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0024` and `SCN-LIVE-024`.
