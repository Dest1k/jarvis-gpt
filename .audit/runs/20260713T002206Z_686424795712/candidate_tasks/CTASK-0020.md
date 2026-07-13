# CTASK-0020 — Dependency/build/offline contract is not reproducible from immutable inputs

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0020`. Runtime check before READY: `SCN-LIVE-008`.

Context files:

- `backend/requirements-surfer.txt`
- `backend/src/jarvis_gpt/agent.py`
- `pyproject.toml`
- `.github/workflows/ci.yml`
- `backend/Dockerfile`
- `frontend/Dockerfile`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Remove obsolete/unused deps, install from one lock, pin image/action digests and document cached offline start separately from rebuild.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
