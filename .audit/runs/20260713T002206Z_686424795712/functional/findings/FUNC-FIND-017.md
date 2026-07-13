# FUNC-FIND-017 — Doctor output exposes the runtime API token

- Category: `INTERNAL_OUTPUT_LEAK`
- Priority: `P1`
- Affected cases: FUNC-0074 docker compose config check
- Profiles: gemma4-turbo
- Surfaces: Doctor/CLI logs

## Sanitized reproduction

- Request: Run diagnostics without exposing secrets.
- Observed: Compose stdout included JARVIS_API_TOKEN; committed evidence uses a redacted derivative only.
- Expected: Tokens and authorization values are redacted before console/log/report output.
- Evidence: evidence/doctor-full-final-sanitized-v2.json

## Root-cause hypothesis

Smoke captures raw docker compose config output without secret filtering.

## Binary acceptance criteria

Canary credentials never appear in doctor stdout/stderr/JSON or persisted logs; regression scan passes.
