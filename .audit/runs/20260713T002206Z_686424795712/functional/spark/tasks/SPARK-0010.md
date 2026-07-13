# SPARK-0010 — Web synthesis does not reliably return a cited usable result

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-010`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/web_surfer.py; backend/src/jarvis_gpt/web_orchestrator.py; backend/src/jarvis_gpt/agent.py; backend/tests/test_web_surfer_integration.py; backend/tests/test_web_orchestrator.py; backend/tests/test_agent.py

## Problem

The requested cited synthesis failed the usability/evidence rubric.

## Harmless reproduction

Replay OP-0038 three times against the controlled public query; validate every cited URL is present, reachable, and supports its adjacent claim, or that one precise unavailability blocker is returned.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Web result grounding is not enforced at final-answer validation.

## Regression test

Add deterministic cited-result and unavailable-adapter fixtures to web-surfer/orchestrator tests plus final-answer assertions in `test_agent.py`. Run the three listed test files.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Three runs contain supported claims with direct URLs, or one precise actionable unavailability message.
