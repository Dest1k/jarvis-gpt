---
id: JARVIS-0020
title: "Dependency/build/offline contract is not reproducible from immutable inputs"
kind: reliability
severity: medium
priority: P2
phase_a_status: static-confirmed
confidence: 0.98
reproducibility: static-proof
components: ["backend/requirements-surfer.txt"]
feature_ids: []
requirement_ids: [REQ-DEPENDENCY-001, REQ-OFFLINE-001]
scenario_ids: [SCN-LIVE-008]
evidence_ids: [EVID-STATIC-002, EVID-STATIC-006, EVID-STATIC-008]
affected_paths: ["backend/requirements-surfer.txt", "backend/src/jarvis_gpt/agent.py", "pyproject.toml", ".github/workflows/ci.yml", "backend/Dockerfile", "frontend/Dockerfile"]
phase_b_scenarios: [SCN-LIVE-008]
candidate_task_ids: [CTASK-0020]
---

# JARVIS-0020 — Dependency/build/offline contract is not reproducible from immutable inputs

## 1. Summary

An active hint installs stale conflicting surfer pins; unused httpx2 expands dev supply chain; CI ignores uv.lock and mutable image/action/apt inputs remain.

## 2. Contract and impact

Requirements: REQ-DEPENDENCY-001, REQ-OFFLINE-001. Clean/offline builds can drift, downgrade browser dependencies or unexpectedly require registries.

## 3. Static evidence

- `backend/requirements-surfer.txt:10-13`
- `backend/src/jarvis_gpt/agent.py:3312-3323`
- `pyproject.toml:7-25`
- `.github/workflows/ci.yml:10-48`
- `backend/Dockerfile:1-47`
- `frontend/Dockerfile:1-10`

Evidence records: EVID-STATIC-002, EVID-STATIC-006, EVID-STATIC-008.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: An active hint installs stale conflicting surfer pins; unused httpx2 expands dev supply chain; CI ignores uv.lock and mutable image/action/apt inputs remain.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Multiple dependency manifests and runtime paths are not governed by one lock/image identity policy.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `reliability` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-008` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Remove obsolete/unused deps, install from one lock, pin image/action digests and document cached offline start separately from rebuild.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0020` and `SCN-LIVE-008`.
