# Reviewer independence levels

Every review result records exactly one level:

| Level | Meaning |
| --- | --- |
| `DETERMINISTIC_ONLY` | No semantic model judgment; machine assertions only. |
| `SAME_MODEL_CLEAN_CONTEXT` | The same model family in a separate clean context. This is context separation, not a different independent model. |
| `DIFFERENT_PROFILE` | A separately configured profile reviewed the packet. |
| `DIFFERENT_MODEL` | A different model reviewed the packet. |
| `DIFFERENT_PROVIDER` | A separately operated provider reviewed the packet. |
| `HUMAN_ADJUDICATED` | A human made the recorded adjudication. |

## Isolation contract

Each semantic reviewer receives one immutable packet containing only:

- sanitized request;
- expected contract;
- actual output;
- bounded sanitized evidence;
- deterministic failure names and source digest.

The reviewer does not receive the other review verdict. It cannot modify the
packet, evidence, repository, or runtime. Each assessment is written to a
different exclusive-create file with its own reviewer/context identity,
independence label, rationale, citations, packet digest, and review digest.

Two reviews with the same review ID or reviewer/context ID cannot be
adjudicated. Two `SAME_MODEL_CLEAN_CONTEXT` results may be useful calibration,
but must never be described as two independent models.

## Adjudication rules

1. Packet digests must match.
2. Any deterministic failure fixes the result at `FAIL`, regardless of the
   semantic votes.
3. Missing bounded evidence yields `INCONCLUSIVE`.
4. Semantic disagreement yields `INCONCLUSIVE` and both assessments remain in
   the adjudication record.
5. Two semantic `PASS` results can yield `PASS` only when rules 1–3 are
   satisfied.
6. Two `FAIL` results yield `FAIL`; two `INCONCLUSIVE` results remain
   `INCONCLUSIVE`.

External providers are optional adapters, not a foundation prerequisite. No
API key or live external reviewer is needed by the offline tests.
