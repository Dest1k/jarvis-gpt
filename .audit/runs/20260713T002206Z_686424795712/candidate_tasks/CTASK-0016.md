# CTASK-0016 — Frontend stream accepts EOF without terminal state and cannot cancel requests

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0016`. Runtime check before READY: `SCN-LIVE-014`.

Context files:

- `frontend/app/page.tsx`
- `frontend/app/page.tsx`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Require exactly one terminal event, persist interrupted state and bind an AbortController to each turn/window.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
