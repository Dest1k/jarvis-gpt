# SPARK-0006 — Raw tool-call envelopes reach rendered assistant output

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-006`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/llm.py; backend/src/jarvis_gpt/api.py; frontend/app/page.tsx; backend/tests/test_agent.py; backend/tests/test_llm.py; backend/tests/test_api_smoke.py

## Problem

Raw call:documents, call:llm.health, call:dispatcher.status, or JSON tool payloads were rendered.

## Harmless reproduction

Replay OP-0025, OP-0028, OP-0036, and OP-0044; scan every NDJSON delta, terminal answer, and rendered DOM string for `call:`, tool JSON keys, roles, tracebacks, and internal schemas.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Tool-shaped output bypasses the final response integrity classifier on some routes.

## Regression test

Add tool-shaped output fixtures to `backend/tests/test_llm.py` and stream/terminal assertions to `backend/tests/test_agent.py` and `test_api_smoke.py`. Run those three files and require zero marker matches.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Known marker scan finds zero tool envelopes in DOM, NDJSON deltas, and terminal answers over all affected cases.
