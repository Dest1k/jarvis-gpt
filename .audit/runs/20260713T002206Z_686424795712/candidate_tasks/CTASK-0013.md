# CTASK-0013 — User regex can block the async event loop beyond cancellation budgets

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0013`. Runtime check before READY: `SCN-LIVE-022`.

Context files:

- `backend/src/jarvis_gpt/tools.py`
- `backend/src/jarvis_gpt/autonomy_executor.py`
- `backend/src/jarvis_gpt/document_surfer.py`
- `backend/src/jarvis_gpt/document_surfer.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Use a safe regex engine/subset or isolated subprocess with hard wall-clock and size budgets.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
