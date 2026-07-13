# CTASK-0023 — Unauthenticated health response exposes absolute runtime path

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0023`. Runtime check before READY: `SCN-LIVE-030`.

Context files:

- `backend/src/jarvis_gpt/api.py`
- `backend/src/jarvis_gpt/config.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Keep unauthenticated liveness minimal; guard detailed health and fail startup on unsafe bind/token combinations.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
