---
id: JARVIS-0005
title: "Bundled browser does not enforce public-only validation on every navigation hop"
kind: defensive-design
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.98
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/web_surfer.py"]
feature_ids: []
requirement_ids: [REQ-URL-001, REQ-TRUST-001]
scenario_ids: [SCN-LIVE-023]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/web_surfer.py", "backend/src/jarvis_gpt/web_surfer.py", "backend/src/jarvis_gpt/web_surfer.py", "backend/src/jarvis_gpt/web_surfer_adapter.py"]
phase_b_scenarios: [SCN-LIVE-023]
candidate_task_ids: [CTASK-0005]
---

# JARVIS-0005 — Bundled browser does not enforce public-only validation on every navigation hop

## 1. Summary

Deep research and shopping accept result strings by HTTP prefix and navigate without the core transport's DNS pinning and per-redirect validation; subresources are not uniformly guarded.

## 2. Contract and impact

Requirements: REQ-URL-001, REQ-TRUST-001. Untrusted public content may cause browser requests to private/link-local destinations.

## 3. Static evidence

- `backend/src/jarvis_gpt/web_surfer.py:522-569`
- `backend/src/jarvis_gpt/web_surfer.py:922-965`
- `backend/src/jarvis_gpt/web_surfer.py:1873-1894`
- `backend/src/jarvis_gpt/web_surfer_adapter.py:249-308`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Deep research and shopping accept result strings by HTTP prefix and navigate without the core transport's DNS pinning and per-redirect validation; subresources are not uniformly guarded.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: The hardened core HTTP path and the Playwright path implement different destination policies.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `defensive-design` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-023` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Apply one public-only resolver/redirect/subresource policy to every browser request and navigation.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0005` and `SCN-LIVE-023`.
