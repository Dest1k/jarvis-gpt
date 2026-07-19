# Current audit handoff

Updated: 2026-07-19 ~14:00 MSK (pre-reboot checkpoint by Grok)

Scope override: all model-facing audit work uses only the live `qwen36-vl` profile.
Gemma is excluded from every live, GUI, API, Telegram, recovery, and soak scenario.

## Machine reboot checkpoint

This handoff was written so progress survives a full host reboot.
After reboot, resume from **Exact next step** below. Do not restart from baseline `c4d7d33`
if the working-tree commit below already landed on `main`.

## Baseline and what was done

- Repository: `D:\jarvis-gpt`, branch `main`.
- Historical remote baseline when work started: `c4d7d33`.
- Large Codex unfinished tree was continued: ownership CAS/nonce, finance card binding,
  chat stream recovery, Telegram durability, dispatcher/launcher crash-safety, Qwen-only policy.

### Closed in this continuation (Grok)

1. **Ownership P1 (dispatcher + launcher)**
   - Python stop supports `expected_operation_nonce` and refuses same-id/wrong-nonce CAS.
   - Explicit reconcile for journal phase `stopped` (tombstone clear on absence / foreign container).
   - PowerShell `Read-DispatcherOwnershipJournal` accepts `launcher_owned=false` and
     `rollback-intent` without container ids; requires bool presence of ownership fields.
   - Contract tests updated for Mandatory `-ExpectedOperationNonce`, nonce mismatch stop,
     fingerprint/stop-stack static checks, encoding-safe PowerShell capture.
   - New tests: wrong-nonce stop refuse, stopped tombstone clear paths.

2. **Finance validator root cause (partial → practical fix)**
   - Instrument cards without `#` headings (Qwen layout `Brent (BZ=F)`) bind via card scope.
   - Quote-time calendar date uses UTC for Zulu timestamps (not Moscow news TZ).
   - Regression: `test_typed_quotes_ground_plain_qwen_cards_without_hash_headings`.

3. **Focused automated verification before reboot**
   - `tests/test_dispatcher.py` + `tests/test_deployment_contracts.py` + finance card tests:
     **92 passed**.
   - Full backend suite was started but **not finished** before the reboot save.
   - Frontend: `tsc --noEmit` OK.
   - Frontend: `node tests/chat-stream-recovery.mjs` **FAILING** — expects upload preflight
     before WAL persist order in `page.tsx` (`durableWrite > uploadPreflight`). Fix before
     treating GUI recovery as green.

## Open / incomplete

1. Fix `frontend/tests/chat-stream-recovery.mjs` failure (submit/upload/WAL order vs `page.tsx`).
2. Full backend `pytest tests/` green (was interrupted for reboot save).
3. Frontend remaining scripts: memory-graph, runtime-identity, stream-placeholder, owner-session, build.
4. Commit/push of this tree (done if git shows post-checkpoint HEAD ≠ `c4d7d33`; verify after reboot).
5. Restart full Qwen stack **only** via `scripts/jarvis-launcher.ps1` after integrated tests.
6. Live smoke: exact completion, Telegram continuity, finance, OpenAPI/GUI recovery.
7. Exhaustive operator audit: 60 Qwen cases / 151 fresh repeats
   (`docs/audit/11_JARVIS_EXHAUSTIVE_LIVE_AUDIT_PROMPT.md` and this run dir).
8. Q059 needs real Telegram owner+guest inbound messages (cannot fabricate).

## Exact next step after reboot

1. `cd D:\jarvis-gpt` → `git status` / `git log -1` — confirm checkpoint commit is present and pushed.
2. Fix chat-stream-recovery test vs `frontend/app/page.tsx` order.
3. `cd backend; uv run --with pytest python -m pytest tests/ -q`
4. Frontend node tests + production build.
5. If green: ensure pushed to `origin/main`.
6. Launcher start full `qwen36-vl` stack; prove live completion `LIVE_OK`.
7. Begin live audit from this functional run directory.

## Protected working-tree items (do not stage)

- `;; esac; done`
- `JARVIS_FULL_AUDIT_PROMPT_FIXED.md`
- `live_test_f99e67/`
- `plan3days.md`
- Historical audit runs under `.audit/runs/` except intentional docs/handoff updates

## Intended commit scope (what to preserve)

Modified tracked files from the ownership/chat/finance/telegram work, plus:
- `frontend/lib/chat-stream-recovery.mjs`
- `frontend/tests/chat-stream-recovery.mjs`
- `docs/audit/11_JARVIS_EXHAUSTIVE_LIVE_AUDIT_PROMPT.md`
- `.audit/.gitattributes` (if present and intentional)
- This handoff under `.audit/runs/20260719T053029Z_c4d7d33/functional/HANDOFF_CURRENT.md`

Do **not** stage protected junk listed above.
