---
id: JARVIS-0006
title: "Browser worker inherits secrets/runtime access while Chromium sandbox is disabled"
kind: defensive-design
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.97
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/web_surfer_adapter.py"]
feature_ids: []
requirement_ids: [REQ-TRUST-001, REQ-SECRET-001]
scenario_ids: [SCN-LIVE-024]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/web_surfer_adapter.py", "backend/src/jarvis_gpt/web_surfer.py", "docker-compose.yml", "backend/tests/test_deployment_contracts.py"]
phase_b_scenarios: [SCN-LIVE-024]
candidate_task_ids: [CTASK-0006]
---

# JARVIS-0006 — Browser worker inherits secrets/runtime access while Chromium sandbox is disabled

## 1. Summary

The worker receives the full environment (including API/search keys), retains writable /runtime access and launches Chromium with --no-sandbox.

## 2. Contract and impact

Requirements: REQ-TRUST-001, REQ-SECRET-001. A browser compromise has a materially wider secret/data blast radius than documented containment implies.

## 3. Static evidence

- `backend/src/jarvis_gpt/web_surfer_adapter.py:846-889`
- `backend/src/jarvis_gpt/web_surfer.py:462-476`
- `docker-compose.yml:82-122`
- `backend/tests/test_deployment_contracts.py:21-39`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: The worker receives the full environment (including API/search keys), retains writable /runtime access and launches Chromium with --no-sandbox.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Process-tree containment was implemented without an environment/filesystem capability boundary; deployment tests inspect the Dockerfile, not actual launch args.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `defensive-design` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-024` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Allowlist worker env, isolate writable roots, enable/verify browser sandboxing and test effective runtime privileges.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0006` and `SCN-LIVE-024`.
