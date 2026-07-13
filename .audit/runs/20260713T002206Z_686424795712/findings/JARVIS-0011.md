---
id: JARVIS-0011
title: "Directory ingest can follow symlinks outside allowed roots and index sensitive files"
kind: defensive-design
severity: high
priority: P1
phase_a_status: static-confirmed
confidence: 0.98
reproducibility: static-proof
components: ["backend/src/jarvis_gpt/ingest.py"]
feature_ids: []
requirement_ids: [REQ-FILES-001, REQ-SECRET-001, REQ-TRUST-001]
scenario_ids: [SCN-LIVE-025]
evidence_ids: [EVID-STATIC-004, EVID-STATIC-014]
affected_paths: ["backend/src/jarvis_gpt/ingest.py", "backend/src/jarvis_gpt/ingest.py", "backend/src/jarvis_gpt/ingest.py", "backend/src/jarvis_gpt/ingest.py"]
phase_b_scenarios: [SCN-LIVE-025]
candidate_task_ids: [CTASK-0011]
---

# JARVIS-0011 — Directory ingest can follow symlinks outside allowed roots and index sensitive files

## 1. Summary

Recursive ingest includes .env/config/log files and resolves file symlinks without rechecking containment after resolution.

## 2. Contract and impact

Requirements: REQ-FILES-001, REQ-SECRET-001, REQ-TRUST-001. A file under an allowed directory may copy/index outside-root data; sensitive content is stored without redaction.

## 3. Static evidence

- `backend/src/jarvis_gpt/ingest.py:26-44`
- `backend/src/jarvis_gpt/ingest.py:81-134`
- `backend/src/jarvis_gpt/ingest.py:240-267`
- `backend/src/jarvis_gpt/ingest.py:370-402`

Evidence records: EVID-STATIC-004, EVID-STATIC-014.

## 4. Hermetic reproduction

Static control/data-flow proof; no production service was started.

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: Recursive ingest includes .env/config/log files and resolves file symlinks without rechecking containment after resolution.

## 6. Runtime confirmation

Not required to prove the code-level violation; PHASE B measures integrated impact.

## 7. Root cause

Confirmed design/code cause: Directory enumeration and per-file canonicalization use different trust-boundary checks.

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `defensive-design` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `SCN-LIVE-025` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

Revalidate resolved targets, reject symlink escapes and define explicit sensitive-file inclusion policy with redacted evidence.

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `CTASK-0011` and `SCN-LIVE-025`.
