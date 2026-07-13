# FUNC-FIND-016 — Doctor returns success when a required test fails

- Category: `FALSE_SUCCESS`
- Priority: `P1`
- Affected cases: FUNC-0074
- Profiles: gemma4-turbo
- Surfaces: jarvis.cmd doctor/scripts/doctor.ps1

## Sanitized reproduction

- Request: Run the full doctor smoke suite.
- Observed: Smoke JSON reported ok=false with one required failure, while jarvis.cmd doctor returned 0.
- Expected: Nonzero exit whenever required_ok is false, with the failing check named.
- Evidence: evidence/doctor-full-final-sanitized-v2.json; evidence/doctor-failure-targeted-clean-env.json

## Root-cause hypothesis

PowerShell doctor wrapper does not propagate smoke.py LASTEXITCODE and launcher injects live JARVIS_HOME into tests.

## Binary acceptance criteria

A forced required failure makes doctor exit nonzero; clean full suite exits zero; tests do not inherit deployment home/profile variables.
