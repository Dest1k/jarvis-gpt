---
id: JARVIS-0018
title: "Documented Compose quick start has no API token bootstrap"
kind: defect
severity: medium
priority: P2
phase_a_status: static-confirmed
confidence: 0.99
reproducibility: static-proof
components: ["README.md"]
feature_ids: []
requirement_ids: [REQ-DOC-001, REQ-API-001]
scenario_ids: [SCN-LIVE-006]
evidence_ids: [EVID-STATIC-013, EVID-STATIC-021]
affected_paths: ["README.md", ".env.example", "docker-compose.yml", "docker-compose.yml", "frontend/app/jarvis-api/[...path]/route.ts"]
phase_b_scenarios: [SCN-LIVE-006]
candidate_task_ids: [CTASK-0018]
---

# JARVIS-0018 — Documented Compose quick start has no API token bootstrap

## 1. Summary

README quick start leaves JARVIS_API_TOKEN empty; the proxy hard-returns 503 without a token and the container client is non-loopback to backend.

## 2. Contract and impact

Requirements: REQ-DOC-001, REQ-API-001. The documented Compose path can build successfully but provide an unusable Command Center.

## 3. Static evidence

- `README.md:181-187`
- `.env.example:52-57`
- `docker-compose.yml:97-99`
- `docker-compose.yml:134-136`
- `frontend/app/jarvis-api/[...path]/route.ts:46-53`

Evidence records: EVID-STATIC-013, EVID-STATIC-021.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: README quick start leaves JARVIS_API_TOKEN empty; the proxy hard-returns 503 without a token and the container client is non-loopback to backend.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Token generation exists only in the PowerShell launcher, not in the documented Compose workflow.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `defect` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-006` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Bootstrap a secret explicitly or fail preflight with an exact setup instruction; test both paths.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0018` and `SCN-LIVE-006`.
