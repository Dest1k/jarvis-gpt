# CTASK-0008 — Autonomy job JSON-array RMW loses concurrency and detached start reports false success

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0008`. Runtime check before READY: `SCN-LIVE-019`.

Context files:

- `backend/src/jarvis_gpt/operations.py`
- `backend/src/jarvis_gpt/api.py`
- `backend/src/jarvis_gpt/autonomy_executor.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Use transactional row/CAS state transitions and return/publish the result of a single admission decision.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
