# Audit journal

## 2026-07-19 05:30 UTC

- Confirmed `HEAD == origin/main == c4d7d33` and preserved all pre-existing untracked files.
- Read the canonical functional procedure after synchronization.
- Created an external checkpoint of the historical functional run.
- Docker Desktop was initially stopped.
- First Qwen start failed because the required local hardened image did not exist; Compose tried
  to pull the non-public `jarvis/vllm-openai:v0.25.1-asyncio-e4f88a8` tag after removing the
  mismatched old container.
- Built the hardened image locally from the pinned base digest. The SHA-verified patch step passed.
- Restarted the full stack; backend, frontend and host bridge are online while Qwen reaches model
  readiness.

## 2026-07-19 09:05 UTC

- Recovered the complete `qwen36-vl` stack after the power loss and proved exact live completion;
  backend, frontend, host bridge, Telegram bridge, and dispatcher were online.
- Verified Telegram SQLite integrity and retained per-owner conversations; `@JBL61R` remains an
  owner and no durable identity for `@Pegas61` was observed.
- Reproduced the finance failure with the live Qwen model and exact stored Yahoo/CFTC evidence.
  Markdown record boundaries, current-date/session binding, and missing typed evidence tuples are
  the identified validator contract defects; remediation is active.
- Operator scope changed to Qwen-only. Removed all queued Gemma operator cases and updated the
  reusable audit prompt so no live/GUI/API/Telegram/recovery/soak model test may switch to Gemma.

## 2026-07-19 10:11 UTC

- Recorded the active continuation after the user requested a durable progress checkpoint.
- Finished the Qwen-only operator plan/catalog: 60 exact cases and 151 mandatory fresh repeats.
- Added the repository Qwen-only model-facing test directive and preserved protected untracked
  artifacts outside the intended patch.
- Implemented GUI write-ahead request IDs and bounded same-ID stream recovery across EOF, network
  loss, and reload; typecheck, all five Node suites, and production build passed independently.
- Closed the false-terminal stream defect: interrupted partial output is a nonterminal checkpoint,
  canonical recovery requires an explicit terminal assistant, and request metadata no longer
  leaks through an outer async-generator yield.
- Added generation-aware backend request-lease reclaim and collision-safe runtime-key handling;
  the final expanded backend regression suite remains in progress.
- Implemented dispatcher write-ahead ownership journaling, full-ID/nonce CAS, tri-state Docker
  identity, verified stop, crash-safe state synchronization, and per-launch backend generation.
  Its initial Qwen/static matrix passed 50 tests; final adversarial review remains in progress.
