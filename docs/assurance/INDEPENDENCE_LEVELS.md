# Reviewer independence levels

Semantic reviews do not supply an independence label. Each immutable review
records its factual `context_id`, 128-bit run nonce, provider, model, profile,
context digest, and packet digest. Retain the context digest out of band when
the context is issued. After the review completes, retain its review digest
before the result enters untrusted storage or transport. Adjudication requires
both positional anchor pairs and then computes exactly one pairwise level:

| Computed level | Required factual difference |
| --- | --- |
| `SAME_MODEL_CLEAN_CONTEXT` | Distinct context IDs and nonces; provider, model, and profile are equal. |
| `DIFFERENT_PROFILE` | Provider and model are equal; profile differs. |
| `DIFFERENT_MODEL` | Provider is equal; model differs. |
| `DIFFERENT_PROVIDER` | Provider differs. |

`DETERMINISTIC_ONLY` and `HUMAN_ADJUDICATED` remain vocabulary for non-semantic
workflows; they are not accepted as semantic-review claims and cannot authorize
semantic `PASS`. Malformed anchors are rejected as invalid harness/CLI input.
Missing, mismatched, swapped, or reused well-formed anchors and repeated context
IDs or nonces make independence unverifiable and force `INCONCLUSIVE` unless an
authoritative deterministic failure already fixes the result at `FAIL`.
Recomputing anchors from files presented for adjudication is not an out-of-band
trust decision.

## Isolation contract

Each semantic reviewer receives one immutable packet containing only:

- sanitized request, expected contract, and actual output;
- bounded sanitized evidence and its substantive `evidence:<id>` catalog;
- sanitized factual assertions and their `assertion:<id>` catalog;
- deterministic failure names and source/evidence/manifest/replay digests.

Each citable bounded-evidence item is an exact typed envelope containing a
supported `kind`, non-empty `assertion_ids` links into the packet, and
substantive `content`. Metadata-only fields such as transport and tags, and
arbitrary unknown scalar keys, do not receive evidence IDs. Malformed attempted
typed envelopes are rejected. `PASS` and `FAIL` require at least one exact
substantive evidence ID and one exact assertion ID; empty, duplicate, wildcard,
parent, and unknown citations are rejected.

Every review uses a new exclusive-create file and binds its context facts,
packet digest, verdict, rationale, exact citations, and complete packet into its
review digest. The separately retained review anchor therefore detects later
semantic edits even if an attacker recomputes the digest stored in the file.
The reviewer does not receive the other verdict and cannot modify the packet,
evidence, repository, or runtime. Adjudication preserves both original review
objects and their digests.

## Adjudication rules

1. Embedded and top-level packet digests must match the same source-verified
   packet.
2. Review IDs and reviewer IDs must be distinct, and both internal review
   digests must verify.
3. Each context digest must equal its separately retained positional anchor.
4. Each complete review digest must equal its separately retained positional
   anchor.
5. Independence is computed from the two anchored factual contexts; repeated
   context or nonce yields `INCONCLUSIVE`.
6. Any deterministic failure fixes the result at `FAIL`, regardless of the
   semantic votes or independence state.
7. Missing substantive evidence yields `INCONCLUSIVE`.
8. Semantic disagreement yields `INCONCLUSIVE` and preserves both assessments.
9. Two supported semantic `PASS` results can yield `PASS` only after rules 1-8.
10. The adjudication writer re-derives the decision from both supplied anchor
    pairs before persistence.

Context and review digests detect mutation relative to retained anchors. They
do not authenticate a provider, model operator, or human identity. External
provider attestations remain optional adapters and are not a foundation
prerequisite; offline tests use typed synthetic contexts and no API key.
