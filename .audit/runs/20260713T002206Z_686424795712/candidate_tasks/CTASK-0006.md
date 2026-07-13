# CTASK-0006 — Browser worker inherits secrets/runtime access while Chromium sandbox is disabled

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0006`. Runtime check before READY: `SCN-LIVE-024`.

Context files:

- `backend/src/jarvis_gpt/web_surfer_adapter.py`
- `backend/src/jarvis_gpt/web_surfer.py`
- `docker-compose.yml`
- `backend/tests/test_deployment_contracts.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Allowlist worker env, isolate writable roots, enable/verify browser sandboxing and test effective runtime privileges.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
