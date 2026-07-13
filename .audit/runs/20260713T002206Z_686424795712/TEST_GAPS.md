# Test gaps

P0/P1 gaps: DB/vault transaction fault injection; approval audit failure and creation redaction; concurrent jobs and same-conversation requests; transport retry; replay eviction; launcher ownership/process scope; model-switch rollback; Compose health/token; browser redirect/subresource/env/sandbox; symlink ingest; hostile regex; archive failure atomicity.

P2 gaps: real stream disconnect/marker cleanup; concurrent WebSocket ordering/replay; frontend stale/error/cancel/service-worker/accessibility; malformed cadence/deadline; web-watch outbox; storage migration/integrity/retention/restore; document compressed-member/memory bounds.

CI gaps: no Linux/Python 3.12 job, Compose render/build, PowerShell behavioral harness, frontend tests/lint, coverage threshold, package build, lock-frozen Python install or deterministic offline gate.
