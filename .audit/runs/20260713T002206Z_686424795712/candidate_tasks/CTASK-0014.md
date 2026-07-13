# CTASK-0014 — Command Center advertises a live WebSocket feed whose transport is disabled

Status: `BLOCKED_BY_SPEC`

Root finding: `JARVIS-0014`. Runtime check before READY: `SCN-LIVE-013`.

Context files:

- `frontend/app/page.tsx`
- `frontend/app/page.tsx`
- `frontend/app/page.tsx`
- `README.md`
- `docs/assistant-notes.md`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Implement authenticated same-origin realtime transport or remove/label the inactive surface consistently.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
