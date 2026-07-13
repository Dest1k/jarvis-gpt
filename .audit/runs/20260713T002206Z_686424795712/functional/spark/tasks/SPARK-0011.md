# SPARK-0011 — Interrupted GUI stream can leave an empty stale assistant bubble

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-011`
- Dependencies: none
- Allowed files: frontend/app/page.tsx; frontend/package.json; backend/src/jarvis_gpt/api.py; backend/tests/test_api_smoke.py

## Problem

Observed runs included an empty 0 ms assistant bubble before retry.

## Harmless reproduction

Start OP-0040, navigate to `about:blank` before terminal state, return to the Command Center, and retry. Compare DOM bubbles with persisted messages and require no empty 0 ms assistant entry.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Frontend stream teardown commits a placeholder before terminal reconciliation.

## Regression test

Add an API stream disconnect/retry assertion to `backend/tests/test_api_smoke.py` and the smallest frontend test foundation in `frontend/package.json` covering placeholder rollback and terminal deduplication. Run the API test and frontend typecheck/test command.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Navigation interruption and retry yield no empty/duplicate/stale final in three repeats and persisted history matches DOM.
