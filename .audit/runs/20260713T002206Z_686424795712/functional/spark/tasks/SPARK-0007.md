# SPARK-0007 — Uploaded document recall is unreliable and blocks missions

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-007`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/document_memory.py; backend/src/jarvis_gpt/document_runtime.py; backend/src/jarvis_gpt/storage.py; backend/src/jarvis_gpt/agent.py; backend/tests/test_document_memory.py; backend/tests/test_document_runtime.py; backend/tests/test_agent.py

## Problem

Existing documents were reported missing or the mission stopped at recall.

## Harmless reproduction

Upload the controlled fixture once, retain its returned ID, then replay OP-0026, OP-0032, OP-0039, and OP-0050 by exact name and ID. Compare upload storage, conversation references, retrieval result, and mission final state.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Filename/source-ID lookup differs between upload, conversation, and agent document routes.

## Regression test

Add fresh/prior-upload lookup and mission document-binding cases to document-memory/runtime tests; assert exact source ID, controlled token, and completed report. Run the three listed test files.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Fresh and previously uploaded files resolve by exact name/ID and complete fact, comparison, retry, and mission cases.
