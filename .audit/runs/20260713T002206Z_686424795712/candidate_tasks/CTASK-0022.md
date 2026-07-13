# CTASK-0022 — Web-watch persists digest before durable notification delivery

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0022`. Runtime check before READY: `SCN-LIVE-021`.

Context files:

- `backend/src/jarvis_gpt/autonomy_executor.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Persist detection plus pending notification atomically and acknowledge only after durable delivery.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
