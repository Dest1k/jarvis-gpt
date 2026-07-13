# FUNC-FIND-012 — Requested memory namespace is ignored

- Category: `STATE_RECOVERY_FAILURE`
- Priority: `P2`
- Affected cases: OP-0041 repeats 1-2
- Profiles: gemma4-turbo
- Surfaces: GUI/Memory/API

## Sanitized reproduction

- Request: Store a controlled marker only in audit.functional.20260713.
- Observed: API records used namespace operator and persisted the marker/persona instruction there.
- Expected: Exact requested namespace, isolated recall, and no persona bleed.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/api-baseline-turbo.json

## Root-cause hypothesis

Persona/memory write route applies a hard-coded default namespace.

## Binary acceptance criteria

Writes and recall use the requested namespace exactly and operator/default namespaces remain unchanged.
