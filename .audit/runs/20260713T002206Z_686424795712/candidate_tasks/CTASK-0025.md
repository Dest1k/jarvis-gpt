# CTASK-0025 — Frontend accessibility and test harness leave interaction regressions unchecked

Status: `BLOCKED_BY_SPEC`

Root finding: `JARVIS-0025`. Runtime check before READY: `SCN-LIVE-031`.

Context files:

- `frontend/app/page.tsx`
- `frontend/app/page.tsx`
- `frontend/package.json`
- `.github/workflows/ci.yml`

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- Add component boundaries and browser/component accessibility tests for critical states.
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
