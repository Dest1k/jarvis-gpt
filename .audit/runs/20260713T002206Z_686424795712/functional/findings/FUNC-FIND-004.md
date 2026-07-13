# FUNC-FIND-004 — Multi-turn references to prior options/files are lost

- Category: `CONTEXT_LOSS`
- Priority: `P2`
- Affected cases: OP-0014, OP-0016, OP-0032
- Profiles: gemma4-turbo
- Surfaces: GUI/Dialog/Documents

## Sanitized reproduction

- Request: Apply a short pronoun or prior-file follow-up inside one conversation.
- Observed: The selected option or earlier uploaded file was not reliably resolved.
- Expected: Only the current conversation's referenced object is used.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv

## Root-cause hypothesis

Conversation grounding and file-reference resolution use inconsistent state sources.

## Binary acceptance criteria

All reference cases resolve the exact prior object across three deterministic repeats without cross-window state.
