# SPARK-0003 — Artifact generation ignores exact paths or returns incomplete transforms

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-003`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/document_agent.py; backend/src/jarvis_gpt/document_runtime.py; backend/tests/test_document_runtime.py; backend/tests/test_tools.py

## Problem

Wrong parent path, raw pseudo-tool output, missing repeats, or literal Markdown in DOCX.

## Harmless reproduction

In a temporary directory, replay OP-0013 and OP-0029..OP-0031, then launch the three OP-0034 windows. Compare exact destination paths, source hashes, artifact hashes, DOCX ZIP/XML structure, and collision-free filenames.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Artifact intent, path binding, and post-write verification are not one atomic contract.

## Regression test

Extend `backend/tests/test_document_runtime.py` with exact-destination, copy-only, Markdown-to-DOCX, and concurrent-name cases; assert source hashes remain unchanged. Run `py -3.11 -m pytest backend/tests/test_document_runtime.py backend/tests/test_tools.py -q`.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Exact-path, copy-only, conversion, and three-way collision tests produce validated artifacts with unchanged sources.
