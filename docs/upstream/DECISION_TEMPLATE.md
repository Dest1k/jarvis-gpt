# Upstream adoption decision: `<candidate_id>`

> Engineering decision record; not legal advice or a claim of model authorship.

## Identity

- Candidate ID:
- Component:
- Reproduced finding(s) or capability gap:
- Origin kind:
- Adoption mode:
- Repository URL:
- Pinned commit SHA:
- Candidate record path and SHA-256:
- Provenance record path and SHA-256:

## Bounded proposal

- Exact upstream source files reviewed:
- Exact repository destinations, if any:
- Intended behavior change:
- Explicitly excluded behavior:
- Isolation boundary used by the spike:

## Evidence review

- Repository license path, identifier, and SHA-256:
- License class and notice result:
- Explicit license review evidence, when required:
- Dependency review evidence and result:
- Security review evidence and result:
- Isolated spike evidence and result:
- Regression tests:
- Failure tests:
- Rollback tests:
- Sanitization confirmation:

## Offline gate

- Validator command:
- Validator version/commit:
- Validator result (`PASS`, `FAIL`, or `BLOCKED`):
- Immutable validator output path and SHA-256:
- Unresolved blockers:

## Human decision

- Decision (`APPROVED`, `REJECTED`, or `PENDING`):
- Approved adoption mode and exact scope:
- Decision maker:
- Decision timestamp (UTC):
- Rationale:
- Required attribution/notices:
- Rollback owner and trigger:
- Follow-up review date or condition:

Approval is invalid if the deterministic gate is `FAIL` or `BLOCKED`, if any
referenced hash does not match, or if the implemented scope exceeds this
record. Popularity is not evidence of suitability. Merge and push remain
separate human-controlled actions.
