# CTASK-0011 — Directory ingest can follow symlinks outside allowed roots and index sensitive files

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0011`. Runtime check before READY: `SCN-LIVE-025`.

Context files:

- `backend/src/jarvis_gpt/ingest.py`
- `backend/src/jarvis_gpt/ingest.py`
- `backend/src/jarvis_gpt/ingest.py`
- `backend/src/jarvis_gpt/ingest.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Revalidate resolved targets, reject symlink escapes and define explicit sensitive-file inclusion policy with redacted evidence.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
