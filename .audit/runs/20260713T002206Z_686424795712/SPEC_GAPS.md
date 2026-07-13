# Specification gaps

1. Whether document/archive/watch mutations require review approval; current default-safe behavior conflicts with file.write.
2. Realtime event transport is simultaneously promised, disabled and presented as waiting.
3. Cached offline start versus clean rebuild/download guarantees and image pull policy.
4. Delete conversation versus erasure of learning/audit copies; global retention/purge policy.
5. Replay/idempotency horizon after bounded outcome eviction.
6. Detailed `/health` exposure for non-loopback deployments.
7. TLS exception policy and shared untrusted-source synthesis rules.
8. Environment precedence across launcher, shell/.env, Compose and backend defaults (including 120s/600s learning interval).
9. What constitutes a compatible/complete custom model and rollback semantics.
10. Active versus legacy `requirements-surfer.txt`, mission handler, localStorage keys and Qwen env names.
