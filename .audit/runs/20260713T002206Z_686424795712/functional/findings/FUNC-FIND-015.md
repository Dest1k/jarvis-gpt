# FUNC-FIND-015 — GUI transcript survives a runtime-home identity change

- Category: `CROSS_SESSION_MIX`
- Priority: `P1`
- Affected cases: FUNC-0085 preflight
- Profiles: gemma4-turbo
- Surfaces: GUI/history/runtime switch

## Sanitized reproduction

- Request: Open the new isolated runtime home with zero backend conversations.
- Observed: The GUI retained a prior transcript while authoritative backend conversation count was zero.
- Expected: Client state is cleared or keyed by backend home/runtime identity.
- Evidence: evidence/gui-stale-state-after-home-switch.json; evidence/backend-conversations-after-home-switch-v2.json

## Root-cause hypothesis

Browser-local chat state is not namespaced by runtime home or backend identity.

## Binary acceptance criteria

Three old-home to new-home switches show zero old messages and DOM/history equal the new backend state.
