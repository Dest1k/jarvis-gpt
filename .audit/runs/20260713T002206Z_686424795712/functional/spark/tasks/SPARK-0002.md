# SPARK-0002 — Exact response constraints are not consistently enforced

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-002`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/verification.py; backend/tests/test_agent.py; backend/tests/test_verification.py

## Problem

Rendered output violated requested count, schema, or assumption contract.

## Harmless reproduction

Replay OP-0007, OP-0010, and OP-0024 from `OPERATOR_TASK_CATALOG.csv`; apply the exact bullet-count, JSON parse/schema, and assumption validators recorded in the catalog.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Final answer validation does not cover ordinary non-tool format contracts.

## Regression test

Add parameterized exact-format cases to `backend/tests/test_verification.py` and agent finalization coverage to `backend/tests/test_agent.py`. Run `py -3.11 -m pytest backend/tests/test_verification.py backend/tests/test_agent.py -q`.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

All listed cases pass their deterministic parser/count validators in three consecutive runs.
