# FUNC-FIND-001 — Direct DNS question is misrouted to shopping

- Category: `RESULT_NOT_USEFUL`
- Priority: `P2`
- Affected cases: OP-0006 repeats 1-2
- Profiles: gemma4-turbo
- Surfaces: GUI/Dialog

## Sanitized reproduction

- Request: Resolve a public hostname and return exactly one sentence.
- Observed: Shopping/catalog workflow ran and did not answer the DNS request.
- Expected: A direct DNS result or an exact actionable network error.
- Evidence: evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv

## Root-cause hypothesis

Intent classification overweights shopping/network catalog terms.

## Binary acceptance criteria

Both deterministic repeats route to DNS/network lookup and return one factual sentence without shopping output.
