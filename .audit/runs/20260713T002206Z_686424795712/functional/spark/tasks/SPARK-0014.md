# SPARK-0014 — Repeated start is not idempotent

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-014`
- Dependencies: none
- Allowed files: scripts/jarvis-launcher.ps1; backend/src/jarvis_gpt/runtime_lease.py; backend/tests/test_runtime_lease.py; backend/tests/test_deployment_contracts.py

## Problem

All three repeats exited 1 with the API executive-state lease message.

## Harmless reproduction

Start the owned isolated turbo stack once, capture PIDs/container ID, then invoke the identical start command three times and compare exit codes, identities, and status output.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Launcher runs a mutating CLI verification after the API acquires the lease.

## Regression test

Add an already-running launcher fixture to `backend/tests/test_deployment_contracts.py`; assert three zero exits, unchanged identities, and no mutating CLI call after lease acquisition. Run deployment and runtime-lease tests.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Three repeat starts return zero, preserve PIDs/container identity, and report already running without lease errors.
