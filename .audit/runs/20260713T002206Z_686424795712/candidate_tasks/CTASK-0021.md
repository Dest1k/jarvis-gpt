# CTASK-0021 — Main storage lacks versioned migration/integrity/retention and policy corruption fails open

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0021`. Runtime check before READY: `SCN-LIVE-029`.

Context files:

- `backend/src/jarvis_gpt/storage.py`
- `backend/src/jarvis_gpt/storage.py`
- `backend/src/jarvis_gpt/storage.py`
- `backend/src/jarvis_gpt/operations.py`
- `backend/src/jarvis_gpt/operations.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Add versioned migrations, strict policy decoding/quarantine, integrity/restore checks and explicit retention/purge semantics.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
