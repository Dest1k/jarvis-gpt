# SPARK-0017 — Doctor output exposes the runtime API token

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-017`
- Dependencies: none
- Allowed files: scripts/smoke.py; scripts/doctor.ps1; backend/src/jarvis_gpt/redaction.py; backend/tests/test_smoke_script.py; backend/tests/test_redaction.py

## Problem

Compose stdout included JARVIS_API_TOKEN; committed evidence uses a redacted derivative only.

## Harmless reproduction

Set a canary `JARVIS_API_TOKEN`, run the Compose-config doctor check, and scan stdout, stderr, JSON report, and saved logs for the exact canary. Use only a disposable canary.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Smoke captures raw docker compose config output without secret filtering.

## Regression test

Add a canary-token Compose-output fixture to `backend/tests/test_smoke_script.py` and shared redaction assertions to `test_redaction.py`; require the canary absent and `<redacted>` present. Run both files.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Canary credentials never appear in doctor stdout/stderr/JSON or persisted logs; regression scan passes.
