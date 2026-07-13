# SPARK-0008 — Corrupt document recovery is inconsistent

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-008`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/document_runtime.py; backend/src/jarvis_gpt/file_types.py; backend/tests/test_document_runtime.py; backend/tests/test_file_types_and_archives.py

## Problem

At least one repeat did not provide a clean actionable error/retry result.

## Harmless reproduction

Replay OP-0033 three times using `corrupt-{repeat}.pdf`, capture the first error, attach the corresponding valid replacement, and compare the retry final against stale partial output.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Parser failure and retry state are not consistently normalized.

## Regression test

Add corrupt-to-valid retry cases to `backend/tests/test_document_runtime.py`; assert one normalized actionable error, no persisted partial result, and one clean retry. Run document-runtime and file-type tests.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Three corrupt-to-valid retries show one actionable error followed by one clean result with no stale content.
