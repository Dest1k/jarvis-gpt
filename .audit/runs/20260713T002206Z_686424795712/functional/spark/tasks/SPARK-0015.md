# SPARK-0015 — GUI transcript survives a runtime-home identity change

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-015`
- Dependencies: none
- Allowed files: frontend/app/page.tsx; frontend/package.json; backend/src/jarvis_gpt/api.py; backend/tests/test_api_smoke.py

## Problem

The GUI retained a prior transcript while authoritative backend conversation count was zero.

## Harmless reproduction

Open a runtime home containing a marked transcript, stop it, start an empty isolated home, reload the same browser tab three times, and compare DOM/history with `GET /api/conversations`.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Browser-local chat state is not namespaced by runtime home or backend identity.

## Regression test

Add runtime-identity-keyed client-state coverage using the frontend test foundation in `frontend/package.json`; assert a home/profile identity change clears stale messages. Retain an API empty-history assertion in `test_api_smoke.py`.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Three old-home to new-home switches show zero old messages and DOM/history equal the new backend state.
