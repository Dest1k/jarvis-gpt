# Behavioral contract

The 22 canonical requirements are in `machine/requirements.jsonl`. They cover API/stream truth, CLI outcomes, tool schemas, exact approvals, idempotency, atomic storage, profile identity, offline core, launcher ownership, frontend freshness, untrusted input, public-only URLs, file roots, archive atomicity, secret minimization, background jobs, resource bounds, reproducible dependencies, test oracles, docs coherence and legacy isolation.

Positive static support includes conditional approval claim, exact mission/action binding, fail-closed interrupted approval recovery, typed execution verification/rollback, durable checksummed replay, process birth identity, deny-by-default execution capabilities, hardened core HTTP public-only transport, upload size cleanup and React rendering without raw HTML.

Conflicts are not resolved by guessing: see `SPEC_GAPS.md`. Nondeterministic LLM behavior remains a semantic PHASE B contract, not exact-string equality.
