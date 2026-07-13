# CTASK-0005 — Bundled browser does not enforce public-only validation on every navigation hop

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0005`. Runtime check before READY: `SCN-LIVE-023`.

Context files:

- `backend/src/jarvis_gpt/web_surfer.py`
- `backend/src/jarvis_gpt/web_surfer.py`
- `backend/src/jarvis_gpt/web_surfer.py`
- `backend/src/jarvis_gpt/web_surfer_adapter.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Apply one public-only resolver/redirect/subresource policy to every browser request and navigation.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
