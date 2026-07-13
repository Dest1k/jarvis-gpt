# CTASK-0007 — JarvisStorage operations can leave a poisoned transaction after exceptions

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0007`. Runtime check before READY: `SCN-LIVE-018`.

Context files:

- `backend/src/jarvis_gpt/storage.py`
- `backend/src/jarvis_gpt/storage.py`
- `backend/src/jarvis_gpt/storage.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Wrap each logical mutation in explicit transaction/rollback and define ordering or an outbox for filesystem mirrors.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
