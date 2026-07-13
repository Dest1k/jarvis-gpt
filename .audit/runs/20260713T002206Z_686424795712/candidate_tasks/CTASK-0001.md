# CTASK-0001 — Model activation accepts unverified directories and has no rollback

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0001`. Runtime check before READY: `SCN-LIVE-011`.

Context files:

- `backend/src/jarvis_gpt/model_hub.py`
- `backend/src/jarvis_gpt/model_catalog.py`
- `backend/src/jarvis_gpt/api.py`
- `frontend/app/page.tsx`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Introduce a manifest/shard/architecture compatibility gate and a staged switch with health confirmation and rollback.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
