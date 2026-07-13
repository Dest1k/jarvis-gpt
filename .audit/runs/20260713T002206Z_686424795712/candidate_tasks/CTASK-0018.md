# CTASK-0018 — Documented Compose quick start has no API token bootstrap

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0018`. Runtime check before READY: `SCN-LIVE-006`.

Context files:

- `README.md`
- `.env.example`
- `docker-compose.yml`
- `docker-compose.yml`
- `frontend/app/jarvis-api/[...path]/route.ts`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Bootstrap a secret explicitly or fail preflight with an exact setup instruction; test both paths.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
