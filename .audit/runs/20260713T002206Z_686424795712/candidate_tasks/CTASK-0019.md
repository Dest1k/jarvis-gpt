# CTASK-0019 — Mutating document/watch tools default to safe and bypass approval

Status: `BLOCKED_BY_SPEC`

Root finding: `JARVIS-0019`. Runtime check before READY: `SCN-LIVE-028`.

Context files:

- `backend/src/jarvis_gpt/tools.py`
- `backend/src/jarvis_gpt/tools.py`
- `backend/src/jarvis_gpt/tools.py`
- `backend/src/jarvis_gpt/tools.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Define danger policy per action and make missing classification fail closed for mutating tools.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
