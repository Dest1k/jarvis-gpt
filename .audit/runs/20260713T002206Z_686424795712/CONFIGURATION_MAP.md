# Configuration map

Precedence is split: explicit CLI profile -> `JARVIS_PROFILE` -> `gemma4-turbo`; launcher overwrites key runtime/profile/API values; Compose reads shell/.env interpolation; backend reads process env; frontend proxy reads server-only backend URL/token. This precedence is not documented as one contract.

Profiles: `gemma4-turbo` -> `gemma4-26b-a4b-nvfp4`; `gemma4-mono-perf` and `gemma4-mono` -> `gemma4-31b-it-nvfp4` with different length/offload flags. Static extraction confirms launcher/config/catalog agree. PHASE B must compare actual dispatcher/UI identity.

Drift: learning interval native default 120s vs 600s in `.env.example`, Compose and runtime docs. Images/actions/base packages use mutable tags; cached-offline start and rebuild/download are distinct but not fully specified. Compose frontend health dependency and token bootstrap have formal findings.
