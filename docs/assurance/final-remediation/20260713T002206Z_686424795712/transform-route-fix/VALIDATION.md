# VALIDATION — final transform-route remediation (RB-5)

## 1. Isolation

| Check | Result |
|-------|--------|
| Worktree | `D:\jarvis-gpt-worktrees\final-transform-route-fix-20260713T002206Z_686424795712` |
| Branch | `fix/final-transform-route/20260713T002206Z_686424795712` |
| Base | `41c22de07c095c6a595589a57614cfbf55d33e48` |
| HEAD == BASE before edits | **PASS** |
| Rollback bundle | `D:\jarvis\audit-backups\...\final-transform-route-fix\pre-remediation.bundle` |
| Production state | not used |
| Push / merge | none |

## 2. Pre-fix attribution (exact base × 12)

Claude fully specified Russian transform prompt:

| Metric | Result |
|--------|--------|
| Intent | `EXISTING_DOCUMENT_REFERENCE` × 12 |
| Task plan | `reasoning` / `local_admin_advice` × 12 |
| Arbiter consulted | yes × 12 |
| Exact artifact | **0/12** |
| Mission (LLM off) | 0 |
| Payload guard (LLM off) | 0 |

Root causes documented in backup `attribution/ROOT_CAUSES.md`:

- **A** single-step transform escalates via misclassification → local bucket → arbiter → mission
- **B** free tool-loop model JSON hits payload guard intended for untrusted protocol text

## 3. Regression tests A–L

File: `backend/tests/test_rb5_transform_route.py` — **13 passed**

| ID | Case | Result |
|----|------|--------|
| A | Fully specified transform 100/100 deterministic routes | PASS |
| B | No generic arbiter for complete transform | PASS |
| C | No mission plan for single-step transform | PASS |
| D | Typed trusted invocation not blocked by payload guard | PASS |
| E | Model-generated tool envelope still blocked | PASS |
| F | Incomplete transform → clarification, no side effects | PASS |
| G | Multi-step composite may mission only with real extra steps | PASS |
| H | Existing-document recall remains recall | PASS |
| I | Direct NEW_ARTIFACT_REQUEST remains generation | PASS |
| J | Exact destination verification RB-4 still required | PASS |
| K | Tool failure no fallback to mission/search | PASS |
| L | Two conversations do not mix transform contracts | PASS |
| + | English + Claude Russian share deterministic path | PASS |

RB-4 suite `test_rb4_transform_path.py`: **14 passed** (unchanged contract).

## 4. Live acceptance (temporary allowed root)

Evidence:
`D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-transform-route-fix\live-results\live-acceptance.json`

| Gate | Required | Measured | Verdict |
|------|----------|----------|---------|
| Exact fully specified transform | 12/12 | 12/12 | **PASS** |
| Original Claude scenario | 6/6 | 6/6 | **PASS** |
| Incomplete transform | 6/6 | 6/6 | **PASS** |
| Clarification follow-up | 3/3 | 3/3 | **PASS** |
| Existing recall | 3/3 | 3/3 | **PASS** |
| Direct new artifact | 3/3 | 3/3 | **PASS** |
| Synthetic internal envelope blocked | 3/3 | 3/3 | **PASS** |
| Source hash unchanged | required | True | **PASS** |

Post-fix attribution × 12: **12/12** transform + artifact + zero arbiter + zero mission.

Deterministic sealed path uses `JARVIS_LLM_ENABLED=0` / FailLLM so route selection
does not depend on model non-determinism.

## 5. Full suites

| Check | Result |
|-------|--------|
| Focused agent/routing/document/tool tests | **380 passed** |
| Full backend pytest | **904 passed, 13 skipped**, ~179 s, exit 0 |
| QA pytest | **218 passed, 3 skipped**, exit 0 |
| `qa.cli validate-suite operator_core` | ok, 1 scenario |
| `qa.cli validate-evidence` | ok, 8 records |
| `qa.cli replay` | ok, 8 cases (1 PASS · 6 FAIL · 1 INCONCLUSIVE), 0 mismatches |
| pinned ruff 0.8.4 `backend/src backend/tests` | **All checks passed, exit 0** |
| `compileall backend/src qa` | **exit 0** |
| `git diff --check` | clean |
| Secret scan over production/test diff | **0 hits** |
| Frontend / scripts / .github / qa trees vs base | **equal** (no re-verify of doctor/frontend journeys) |

## 6. Out of scope (not re-run)

- Full doctor journey (trees untouched; previous PASS reused)
- Frontend npm build (frontend tree equal)
- 31B models (forbidden)
- Production user documents / production state
- Push / merge / attestation / READY

## 7. Candidate status

```text
FINAL_TRANSFORM_ROUTE_CANDIDATE_FOR_REVIEW
```
