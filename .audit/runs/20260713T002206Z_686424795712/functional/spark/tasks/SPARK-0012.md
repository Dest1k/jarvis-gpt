# SPARK-0012 — Requested memory namespace is ignored

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-012`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/cognitive_memory.py; backend/src/jarvis_gpt/persona.py; backend/src/jarvis_gpt/api.py; backend/tests/test_cognitive_memory.py; backend/tests/test_persona.py; backend/tests/test_api_smoke.py

## Problem

API records used namespace operator and persisted the marker/persona instruction there.

## Harmless reproduction

Replay OP-0041 repeats 1-2 with namespace `audit.functional.20260713`; query memory/persona APIs before and after and compare stored namespace plus default `operator` state.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Persona/memory write route applies a hard-coded default namespace.

## Regression test

Add explicit-namespace write/recall/isolation cases to `backend/tests/test_cognitive_memory.py` and API/persona tests; assert no default-namespace or persona mutation. Run the three listed test files.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Writes and recall use the requested namespace exactly and operator/default namespaces remain unchanged.
