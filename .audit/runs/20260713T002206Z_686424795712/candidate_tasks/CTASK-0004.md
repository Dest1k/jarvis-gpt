# CTASK-0004 — Compose frontend waits for a backend healthcheck that does not exist

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0004`. Runtime check before READY: `SCN-LIVE-007`.

Context files:

- `docker-compose.yml`
- `docker-compose.yml`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Add a pinned backend healthcheck or use a condition whose semantics match the service definition, then test rendered Compose in CI.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
