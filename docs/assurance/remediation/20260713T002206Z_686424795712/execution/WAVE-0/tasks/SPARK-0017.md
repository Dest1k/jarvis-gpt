# SPARK-0017 — Doctor token redaction

- Status: `PASS`
- Finding: `FUNC-FIND-017`
- Runtime impact: `code_only`
- Pre-task HEAD: `2fc7c7df15561ac3f8659f7c8c7ec529f87b2de8`
- Checkpoint tag: `pre-functional-20260713T002206Z_686424795712-SPARK-0017`

## Finding

Compose stdout included `JARVIS_API_TOKEN`. Smoke captured raw `docker compose config` output without secret filtering.

## Before (FAIL reproduction)

Isolated canary fixture via mocked `subprocess.run`:

- Canary: `CANARY_TOKEN_SPARK0017_deadbeef`
- `smoke.run("docker compose config", ...)` returned canary in `stdout_tail`
- `PRE_PATCH_FAIL_REPRODUCED: True`

## Patch

Minimal fix in allowed files only:

1. `scripts/smoke.py`
   - Bootstrap `backend/src` for `jarvis_gpt.redaction`
   - `safe_tail()` applies `redact_text` before storing command tails
   - `_failed_check` redacts `error`
   - Final JSON report passed through `redact_value` before print
2. Regression tests in `backend/tests/test_smoke_script.py` and `backend/tests/test_redaction.py`

No changes to `scripts/doctor.ps1` or `backend/src/jarvis_gpt/redaction.py` required: doctor only invokes smoke; existing redaction patterns already cover `*token*` assignments.

## After

- Same canary fixture: canary count `0` in stdout/stderr/JSON
- Redacted marker `[redacted]` present on `JARVIS_API_TOKEN` assignments
- Process exit and machine `ok` agree for controlled required-success path

## Commands and results

```text
py -3.11 -B -m pytest backend/tests/test_smoke_script.py backend/tests/test_redaction.py -q
→ 10 passed

py -3.11 -B -m pytest backend/tests/test_smoke_script.py backend/tests/test_redaction.py backend/tests/test_config_storage.py -q
→ 26 passed

py -3.11 -B -m qa.cli validate-suite qa\suites\operator_core
→ ok=true, scenarios=1

Bounded doctor/smoke journey with disposable canary
→ canary_count=0, redacted_marker=True, exit=0, report_ok=True
```

## Scope

Changed paths:

- `scripts/smoke.py`
- `backend/tests/test_smoke_script.py`
- `backend/tests/test_redaction.py`
- `docs/assurance/remediation/20260713T002206Z_686424795712/execution/WAVE-0/tasks/SPARK-0017.md`
- `docs/assurance/remediation/20260713T002206Z_686424795712/execution/WAVE-0/MANIFEST.yml` (wave execution index)

## Cleanup / rollback

- Task-owned fixtures under external backup `tmp/spark-0017` only
- No production home/credentials used
- `.audit/**` external content manifests unchanged (difference_count=0)
- Rollback: restore from tag `pre-functional-20260713T002206Z_686424795712-SPARK-0017` or revert this commit

## Residual risks

- Live `docker compose config` not re-run against a real daemon in this task; contract covered by fixture matching observed compose YAML shape from sanitized evidence
- Marker form is `[redacted]` (shared redaction library); sanitized audit evidence historically used `<redacted>` as a post-processing token — both exclude the canary value
