# CTASK-0010 — Transport retries can repeat actions after replay retention or new chat authorization

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0010`. Runtime check before READY: `SCN-LIVE-016`.

Context files:

- `backend/src/jarvis_gpt/models.py`
- `backend/src/jarvis_gpt/agent.py`
- `backend/src/jarvis_gpt/execution_replay.py`
- `backend/src/jarvis_gpt/execution_kernel.py`
- `backend/src/jarvis_gpt/tools.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Require request IDs, serialize conversation turns and retain non-replay tombstones beyond result-detail retention.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
