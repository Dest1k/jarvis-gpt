# SPARK-0005 — Ambiguous request creates a mission instead of one precise question

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-005`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/executive_planner.py; backend/tests/test_agent.py; backend/tests/test_executive_planner.py

## Problem

A mission plan was created before the ambiguity was resolved.

## Harmless reproduction

Submit OP-0023 in a clean conversation and inspect conversations, missions, and files before answering. The pre-fix run creates a mission instead of returning exactly one question.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Mission routing precedes clarification gating.

## Regression test

Add `test_ambiguity_blocks_mission_until_one_clarification` to `backend/tests/test_agent.py`; assert zero mission/artifact writes before the answer and correct resume afterward. Run agent and executive-planner tests.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

No mission/artifact is created before one exact clarification; the follow-up resumes the original goal.
