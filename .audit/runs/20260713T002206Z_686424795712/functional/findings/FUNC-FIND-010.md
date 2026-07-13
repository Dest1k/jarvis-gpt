# FUNC-FIND-010 — Web synthesis does not reliably return a cited usable result

- Category: `RESULT_NOT_USEFUL`
- Priority: `P2`
- Affected cases: OP-0038 repeats 1-3
- Profiles: gemma4-turbo
- Surfaces: GUI/Internet

## Sanitized reproduction

- Request: Synthesize a public result with reachable citations.
- Observed: The requested cited synthesis failed the usability/evidence rubric.
- Expected: A concise factual synthesis with direct citations or an exact blocker.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv

## Root-cause hypothesis

Web result grounding is not enforced at final-answer validation.

## Binary acceptance criteria

Three runs contain supported claims with direct URLs, or one precise actionable unavailability message.
