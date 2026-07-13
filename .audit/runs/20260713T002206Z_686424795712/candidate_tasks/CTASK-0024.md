# CTASK-0024 — Bundled web synthesis/TLS trust policy is weaker than the core path

Status: `BLOCKED_BY_SPEC`

Root finding: `JARVIS-0024`. Runtime check before READY: `SCN-LIVE-024`.

Context files:

- `backend/src/jarvis_gpt/agent.py`
- `backend/src/jarvis_gpt/agent.py`
- `backend/src/jarvis_gpt/agent.py`
- `backend/src/jarvis_gpt/web_surfer.py`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Reuse one untrusted-content prompt/provenance contract and fail closed on TLS unless a narrowly approved exception exists.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
