# Functional campaign journal

## 2026-07-13T16:08:36Z — resume and isolation

- Verified Git root `D:/jarvis-gpt`, branch `main`, HEAD `3fda655e4f723a0d8f58a4edfb4b3ee7dda079fe`.
- Captured the pre-existing untracked state and copied the complete static run into the external checkpoint.
- Recorded processes, candidate audit processes, Docker containers/networks, and listening ports before new functional checks.
- Fetched `origin`; local and upstream HEAD were identical, so no fast-forward was required.
- Re-read the complete functional acceptance prompt after fetch.
- Created a new `functional/` namespace without touching prior `.audit/**` artifacts.
- Confirmed static baseline drift to current HEAD is limited to `docs/audit/**`.


## 2026-07-13T19:01:17.602729+00:00 — campaign complete

- Completed 86 scenarios and all 169 operator repeat keys.
- Accepted operator repeat totals: FAIL=49, INCONCLUSIVE=74, PASS=46.
- Scenario totals: BLOCKED_BY_ENV=1, BLOCKED_BY_SPEC=1, FAIL=30, INCONCLUSIVE=34, PASS=20.
- Created 17 reproduced findings and 17 consistent Spark tasks.
- `functional/READY` remains absent; `functional/spark/READY` is present.
- Isolated runtime was returned to the initial offline state; generated home retained for evidence.
