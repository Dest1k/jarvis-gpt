# FUNC-FIND-003 — Artifact generation ignores exact paths or returns incomplete transforms

- Category: `CLAIMED_ARTIFACT_MISSING`
- Priority: `P1`
- Affected cases: OP-0013, OP-0029..OP-0031, OP-0034
- Profiles: gemma4-turbo
- Surfaces: GUI/Documents/filesystem

## Sanitized reproduction

- Request: Create or transform a controlled artifact at the exact requested destination.
- Observed: Wrong parent path, raw pseudo-tool output, missing repeats, or literal Markdown in DOCX.
- Expected: Exact path/type, source preservation, distinct concurrent outputs, and valid native document structure.
- Evidence: evidence/turbo-artifact-validation.json; evidence/TURBO_ARTIFACT_VALIDATION.md; evidence/gui_operator_runs.jsonl

## Root-cause hypothesis

Artifact intent, path binding, and post-write verification are not one atomic contract.

## Binary acceptance criteria

Exact-path, copy-only, conversion, and three-way collision tests produce validated artifacts with unchanged sources.
