# CTASK-0012 — Failed archive extraction leaves partial final outputs

Status: `STATIC_ONLY_REVIEW_REQUIRED`

Root finding: `JARVIS-0012`. Runtime check before READY: `SCN-LIVE-026`.

Context files:

- `backend/src/jarvis_gpt/archive_runtime.py`
- `backend/src/jarvis_gpt/archive_runtime.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Extract into a unique staging root, validate fully, atomically publish, and remove staging on every failure.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
