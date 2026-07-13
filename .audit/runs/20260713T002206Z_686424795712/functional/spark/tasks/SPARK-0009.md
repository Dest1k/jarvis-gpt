# SPARK-0009 — Approved safe action uses a non-canonical tool schema

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-009`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/approval_executor.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/agent.py; backend/tests/test_approval_executor.py; backend/tests/test_tools.py; backend/tests/test_agent.py

## Problem

Approval execution referenced filesystem.mkdir while the allowed action is fs.mkdir; no directory was created.

## Harmless reproduction

With a temporary target path, submit OP-0037, inspect the pending action name/payload, approve it once, execute it once, and verify the directory plus approval/tool audit records.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Model-facing aliases are not canonicalized before approval schema validation.

## Regression test

Add alias-to-canonical `fs.mkdir` approval tests to `backend/tests/test_approval_executor.py` and agent/tool coverage; reject unknown aliases before creating a pending approval. Run the three listed test files.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Three end-to-end approvals bind the canonical action and create only the approved path exactly once.
