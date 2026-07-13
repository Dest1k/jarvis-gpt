# SPARK-0016 — Doctor returns success when a required test fails

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-016`
- Dependencies: none
- Allowed files: scripts/doctor.ps1; scripts/smoke.py; scripts/jarvis-launcher.ps1; backend/tests/test_smoke_script.py; backend/tests/test_config_storage.py

## Problem

Smoke JSON reported ok=false with one required failure, while jarvis.cmd doctor returned 0.

## Harmless reproduction

Run `jarvis.cmd doctor` with the isolated `JARVIS_HOME`; parse the JSON and compare `.ok` with the process exit code. Then run the failing storage test with JARVIS_HOME/PROFILE/MODEL_ROOT removed.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: PowerShell doctor wrapper does not propagate smoke.py LASTEXITCODE and launcher injects live JARVIS_HOME into tests.

## Regression test

Extend `backend/tests/test_smoke_script.py` to force one required failure and assert nonzero propagation through doctor; assert test subprocesses receive a sanitized environment. Run smoke-script and config-storage tests.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

A forced required failure makes doctor exit nonzero; clean full suite exits zero; tests do not inherit deployment home/profile variables.
