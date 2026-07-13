# CTASK-0017 — Generated document collision logic reuses an existing timestamped path

Status: `STATIC_ONLY_REVIEW_REQUIRED`

Root finding: `JARVIS-0017`. Runtime check before READY: `SCN-LIVE-027`.

Context files:

- `backend/src/jarvis_gpt/document_surfer.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Use exclusive create/UUID/counter retry and apply the same rule to archive output directories.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
