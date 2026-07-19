# Qwen-only operator acceptance plan

## Scope

- Run namespace: `.audit/runs/20260719T053029Z_c4d7d33/functional`.
- Every model-facing case uses the already configured live `qwen36-vl` profile.
- Do not switch profiles during GUI, API, CLI, Telegram, recovery, or soak work.
- Execute all 60 catalog cases from fresh state after the integrated restart: 31 critical cases
  three times and 29 remaining cases twice, for exactly 151 executions.
- Existing baseline observations are context only. They do not reduce the fresh repeat count.
- Every case is a real user-surface journey. API, CLI, storage, logs, and deterministic scripts
  provide supporting evidence but never replace the GUI or Telegram surface named by the case.

## Repeat schedule

- Three repeats: `Q006,Q007,Q010,Q014,Q015,Q016,Q019,Q020,Q021,Q023,Q024,Q033,
  Q034,Q035,Q036,Q039,Q040,Q041,Q042,Q043,Q044,Q045,Q047,Q050,Q051,Q054,
  Q055,Q056,Q057,Q058,Q059`.
- Two repeats: `Q001,Q002,Q003,Q004,Q005,Q008,Q009,Q011,Q012,Q013,Q017,Q018,
  Q022,Q025,Q026,Q027,Q028,Q029,Q030,Q031,Q032,Q037,Q038,Q046,Q048,Q049,
  Q052,Q053,Q060`.
- Use a new synthetic marker for every repeat: `Qxxx-Rn-<UTC>-<nonce>`.
- A failed repeat remains a failure. Later passes do not erase it; create a finding and apply the
  documented disposition before any post-fix rerun.

## Preconditions and baseline

1. Record HEAD, dirty-state summary, OS/tool versions, launcher state, exact immutable dispatcher
   container ID, loaded model identity, API/GUI URLs, and UTC/MSK time in
   `ENVIRONMENT_BASELINE.md`.
2. Prove `qwen36-vl` through launcher status, `/health`, `/api/status`, dispatcher state, and one
   exact live completion. A mismatch is a stop condition.
3. Create a consistent SQLite checkpoint with the supported backup command before recovery or
   state-mutating groups. Never copy a live SQLite database with ordinary file copy.
4. Record hashes and sizes of all fixtures before upload. Existing user files, conversations,
   memories, missions, approvals, and Telegram history are out of scope for mutation.
5. Open one authenticated owner browser context. Open a separate isolated guest context only for
   `Q054` and `Q058`; shared cookies or storage invalidate those cases.
6. For `Q059`, identify the two real Telegram principals by immutable numeric IDs. Store only
   redacted identifiers in evidence and never store bot, API, bridge, or user-session secrets.

## Synthetic fixtures

Create fixtures only below `functional/evidence/operator/fixtures/` and use unique names:

- UTF-8 text with a unique marker and three known facts;
- two documents with deliberately distinct facts and one controlled disagreement;
- CSV table with fixed headers, row count, totals, Unicode, and empty-cell cases;
- Markdown source for format conversion;
- a copied document whose original SHA-256 is recorded before any edit;
- a malformed PDF-like file and a clearly unsupported binary file;
- two different source documents requesting the same output filename;
- a bounded timeout helper that can affect only its own audit-owned marker;
- audit-owned output paths for file, approval, mission, memory, and recovery cases.

Fixtures contain no real credentials, personal data, production paths, or network tokens. Any
generated artifact must remain inside an explicitly recorded audit-owned path.

### Template substitution

- `{{MARKER}}` is the exact per-repeat marker defined above.
- `{{TEXT_FIXTURE}}`, `{{UNIQUE_FACT_FIXTURE}}`, `{{DOC_A}}`, `{{DOC_B}}`,
  `{{CSV_FIXTURE}}`, `{{MARKDOWN_FIXTURE}}`, `{{ORIGINAL_DOC}}`, `{{MALFORMED_PDF}}`,
  `{{TIMEOUT_HELPER}}`, and `{{MISSING_FIXTURE}}` resolve to absolute paths recorded with their
  expected SHA-256 before the case starts.
- `{{MISSION_ID}}` and any approval/session identifier resolve only to the exact audit-owned ID
  created by the paired prerequisite in the same repeat.
- Render a catalog request by literal placeholder replacement only. Save both the catalog template
  and the fully rendered user-visible request in `request.json`; unresolved placeholders are a
  `BLOCKED_BY_SPEC` result, not permission to improvise.

## Execution procedure

For each `Qxxx/Rn` row in `OPERATOR_ACCEPTANCE_RESULTS.csv`:

1. Confirm the profile and model identity have not drifted.
2. Capture the exact pre-state required by the catalog validators.
3. Submit the exact `user_request` through the catalog surface using the stated conversation
   shape and constraints. Do not silently improve or reinterpret the prompt during execution.
4. Capture the complete user-visible response, stream reconstruction, conversation/message IDs,
   relevant API/tool/audit events, side effects, and post-state.
5. Run every deterministic validator. A claimed artifact or mutation must be proven by exact path,
   identity, hash, status, and audit linkage.
6. Scan user-visible output for internal protocol markers, raw transport frames, secrets,
   traceback, duplicate terminal output, truncation, and false-success wording.
7. Save evidence before scoring, then update the single results row. Never write secrets or raw
   private user data to audit artifacts.

Browser automation may perform ordinary clicks and input, but screenshots and the resulting DOM,
API, and storage state must prove the real GUI journey. `Q019` and `Q035` require simultaneous
windows. `Q040` requires the real scoped approval control. `Q060` requires resize, zoom, keyboard,
focus, scroll, and long-content evidence. `Q059` requires real inbound messages from both Telegram
accounts; fabricated updates or direct backend calls do not qualify.

## Evidence layout

Use one immutable directory per execution:

```text
evidence/operator/Qxxx/run-n/
  request.json
  response.txt
  deterministic.json
  state-before.json
  state-after.json
  events.jsonl
  screenshot-*.png
  artifacts.json
  review-a.json
  review-b.json
```

Only create files that apply to the case; `deterministic.json`, `request.json`, `response.txt`, and
the two review files are mandatory. Append one sanitized machine record per execution to
`machine/operator_acceptance.jsonl`. Refer to evidence by relative ID from the results CSV.

## Two independent review passes

After deterministic evidence is frozen, perform two independent rubric passes without exposing
one review to the other. Each reviewer scores 0, 1, or 2 for:

- intent fidelity;
- task completion;
- constraint adherence;
- truthfulness;
- response integrity;
- state consistency;
- recovery quality;
- UX clarity.

Deterministic failures cannot be overruled by a reviewer. Reviewer disagreement produces
`INCONCLUSIVE` until reconciled against evidence; it never becomes an automatic PASS.

## Cleanup and rollback

- Track every created conversation, file, approval, mission, memory, process session, and temporary
  network rule by exact immutable ID and owner marker.
- Cancel or execute only the exact audit-owned approval. Delete only audit-owned objects through
  their supported scoped APIs and approval flow.
- Recovery actions use supported launcher commands and exact owned PIDs/container IDs. Never use
  broad kill, prune, reset, recursive cleanup, or an unresolved path.
- Network-fault rules must be uniquely named, snapshotted before creation, bounded to the intended
  process/service, removed in `finally`, and verified absent afterward.
- Restore the normal Qwen stack, run the live completion, API/GUI smoke, Telegram-store integrity,
  SQLite `quick_check` and foreign-key checks, then record the final state.

## Stop and readiness gates

Stop the affected group and record an exact blocker when any of these occurs:

- active profile/model/container identity differs from the Qwen baseline;
- a checkpoint or rollback cannot be proven before a destructive/recovery action;
- a target path, PID, container ID, tenant, Telegram principal, or approval is ambiguous;
- evidence would expose a secret or real private content;
- the system produces cross-session mixing, false success, missing claimed artifacts, internal
  protocol leakage, or an unbounded restart/resource-growth loop.

Do not create `functional/READY` while any results row is `NOT_RUN`, any repeat lacks evidence or
both reviews, any FAIL lacks a finding and disposition, any P0/P1 case is unresolved, fewer than
90 percent of eligible non-P0/P1 cases pass, or the stack has not been restored and re-proven.
