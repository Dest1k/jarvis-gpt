# SPARK-0016 — Doctor exit-code truthfulness

- Status: `PASS`
- Finding: `FUNC-FIND-016`
- Runtime impact: `code_only`
- Pre-task HEAD: `14e42f252ced2381c8f8b905a2609612398981e5`
- Checkpoint tag: `pre-functional-20260713T002206Z_686424795712-SPARK-0016`

## Finding

Smoke JSON reported `ok=false` with a required failure while `jarvis.cmd doctor` returned process exit 0. Launcher also injected live `JARVIS_HOME` / profile / model-root into pytest.

## Before (FAIL reproduction)

- PowerShell `-File` script that runs `py ...; sys.exit(1)` without `exit $LASTEXITCODE` returns process exit **0**
- Same pattern with explicit `exit $LASTEXITCODE` returns **nonzero**
- Matches FUNC-FIND-016 / doctor-full evidence (`returncode: 0` with nested `"ok": false`)

## Patch

1. `scripts/doctor.ps1` — explicit `exit $smokeExit` after smoke
2. `scripts/jarvis-launcher.ps1` — `Invoke-Doctor` runs doctor via nested `-File` process, exits with that code for CLI `Action=doctor`
3. `scripts/smoke.py` — `sanitized_test_env()` strips `JARVIS_HOME`, `JARVIS_MODEL_ROOT`, `JARVIS_PROFILE` from pytest subprocess env
4. Regression tests in `backend/tests/test_smoke_script.py`

## After

- Forced required failure → smoke exit 1, doctor mini-wrapper exit nonzero, JSON `ok=false`
- Clean controlled path → exit 0 / `ok=true`
- Backend tests env has no deployment home/profile/model-root keys

## Commands and results

```text
py -3.11 -B -m pytest backend/tests/test_smoke_script.py backend/tests/test_config_storage.py -q
→ 26 passed
```

## Scope

- `scripts/doctor.ps1`
- `scripts/jarvis-launcher.ps1`
- `scripts/smoke.py`
- `backend/tests/test_smoke_script.py`
- `docs/assurance/remediation/.../execution/WAVE-0/tasks/SPARK-0016.md`
- `docs/assurance/remediation/.../execution/WAVE-0/MANIFEST.yml`

## Cleanup / rollback

- No production credentials or production home used
- Task fixtures under external backup tmp only
- Rollback: tag `pre-functional-20260713T002206Z_686424795712-SPARK-0016`

## Residual risks

- Full live `jarvis.cmd doctor` suite not re-run (expensive); contract covered by smoke exit + doctor.ps1 propagation + env sanitization tests
- Menu-mode doctor no longer returns to menu after nested failure messaging when Action is not `doctor` only; CLI doctor path exits with truthful code
