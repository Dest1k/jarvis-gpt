# CTASK-0002 — Launcher stop fails open when ownership state is missing or corrupt

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0002`. Runtime check before READY: `SCN-LIVE-003`.

Context files:

- `scripts/jarvis-launcher.ps1`
- `scripts/jarvis-launcher.ps1`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Fail closed on absent/corrupt state; persist state atomically with runtime identity and verify it before stop.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
