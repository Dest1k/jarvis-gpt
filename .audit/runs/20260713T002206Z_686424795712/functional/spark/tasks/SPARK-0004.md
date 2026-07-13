# SPARK-0004 — Multi-turn references to prior options/files are lost

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-004`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/storage.py; backend/src/jarvis_gpt/document_memory.py; backend/tests/test_agent.py; backend/tests/test_document_memory.py

## Problem

The selected option or earlier uploaded file was not reliably resolved.

## Harmless reproduction

Create one conversation per OP-0014, OP-0016, and OP-0032, complete the first turn, then send the catalog follow-up without restating the object. Record selected option/file ID and final answer.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Conversation grounding and file-reference resolution use inconsistent state sources.

## Regression test

Add pronoun, selected-option, and prior-file-ID tests to `backend/tests/test_agent.py` and `backend/tests/test_document_memory.py`; assert only the conversation-local object resolves. Run both files with pytest.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

All reference cases resolve the exact prior object across three deterministic repeats without cross-window state.
