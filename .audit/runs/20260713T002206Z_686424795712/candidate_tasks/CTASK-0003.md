# CTASK-0003 — Launcher process cleanup signature can match unrelated processes

Status: `AWAITING_PHASE_B`

Root finding: `JARVIS-0003`. Runtime check before READY: `SCN-LIVE-004`.

Context files:

- `scripts/jarvis-launcher.ps1`
- `scripts/jarvis-launcher.ps1`
- `scripts/jarvis-launcher.ps1`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Track exact child identities and terminate only verified descendants owned by the active launcher state.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
