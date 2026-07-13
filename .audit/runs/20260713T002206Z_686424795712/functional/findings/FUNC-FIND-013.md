# FUNC-FIND-013 — Both 31B profiles are functionally unusable on the certified machine

- Category: `PROFILE_MISMATCH`
- Priority: `P1`
- Affected cases: OP-0045..OP-0068; FUNC-0069; FUNC-0078; FUNC-0083
- Profiles: gemma4-mono-perf;gemma4-mono
- Surfaces: Launcher/provider/API/GUI

## Sanitized reproduction

- Request: Run ordinary profile-specific answers, tools, documents, missions, and web tasks.
- Observed: Direct probes emitted repeated 'cyclic'; GUI returned 400 fallbacks or timed out; mono startup crossed 20 minutes and decoded near 0.1-0.4 tok/s.
- Expected: Correct bounded answer, exact profile identity, and practically usable readiness/latency.
- Evidence: evidence/mono-perf-direct-model-probes.json; evidence/mono-direct-model-probes-ready.json; evidence/mono-startup-timeout-20m.json; evidence/PROFILE_SEMANTIC_REVIEW_RECONCILIATION.csv

## Root-cause hypothesis

31B NVFP4 runtime/profile parameters are incompatible with model quality and practical context/latency on this host.

## Binary acceptance criteria

Three direct probes per profile answer correctly; all profile gate cases complete without fallback; startup and latency meet an explicit contract.
