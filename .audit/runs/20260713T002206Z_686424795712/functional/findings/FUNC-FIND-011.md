# FUNC-FIND-011 — Interrupted GUI stream can leave an empty stale assistant bubble

- Category: `STATE_RECOVERY_FAILURE`
- Priority: `P1`
- Affected cases: OP-0040 repeats 1-3
- Profiles: gemma4-turbo
- Surfaces: GUI/stream/reconnect

## Sanitized reproduction

- Request: Interrupt navigation during a stream, reconnect, and retry.
- Observed: Observed runs included an empty 0 ms assistant bubble before retry.
- Expected: Cancelled partial state is removed or labelled; retry produces one terminal answer.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv

## Root-cause hypothesis

Frontend stream teardown commits a placeholder before terminal reconciliation.

## Binary acceptance criteria

Navigation interruption and retry yield no empty/duplicate/stale final in three repeats and persisted history matches DOM.
