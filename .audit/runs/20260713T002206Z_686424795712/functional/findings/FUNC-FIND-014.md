# FUNC-FIND-014 — Repeated start is not idempotent

- Category: `STARTUP_FAILURE`
- Priority: `P2`
- Affected cases: FUNC-0070, three warm repeats
- Profiles: gemma4-turbo
- Surfaces: Launcher/CLI

## Sanitized reproduction

- Request: Start an already running owned stack.
- Observed: All three repeats exited 1 with the API executive-state lease message.
- Expected: Idempotent success/no-op with truthful already-running status, or an explicit documented contract.
- Evidence: evidence/startup-gemma4-turbo-warm.json; evidence/startup-gemma4-turbo-warm-r2.json; evidence/startup-gemma4-turbo-warm-r3.json

## Root-cause hypothesis

Launcher runs a mutating CLI verification after the API acquires the lease.

## Binary acceptance criteria

Three repeat starts return zero, preserve PIDs/container identity, and report already running without lease errors.
