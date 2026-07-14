# RB-6 Remediation — Clarified Transform Continuation

| Field | Value |
|-------|-------|
| RUN_ID | `20260713T002206Z_686424795712` |
| BASE_CANDIDATE | `d2372de0e7c3c5e6d3c3314f3ec489e618474946` |
| FIX commit | `956411609f04977ce2625942415b383095f98aa8` |
| ASSURANCE commit | branch tip (`assurance: record clarified transform continuation`) |
| Branch | `fix/final-transform-followup/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-transform-followup-fix-20260713T002206Z_686424795712` |
| Status | **FINAL_TRANSFORM_FOLLOWUP_CANDIDATE_FOR_REVIEW** |

## Problem (RB-6)

After an incomplete transform request correctly asked one clarification, the
operator follow-up (format + exact destination) restored a free-text generate
path via `_artifact_spec_from_clarification_resume`. That helper:

1. Scanned the combined original goal + follow-up text.
2. Picked the **first** filename token — the **source** `.txt`.
3. Preferred format from `.txt` over the operator's markdown answer.
4. Called `documents.generate` with `output_name = <source>` and claimed success.

Requested destination was never created. Source filename was written under
`document-outputs` and the final answer falsely claimed success for that name.

Attribution: regression relative to `41c22de0` on the clarified-transform route
after RB-5 sealed fully specified direct transforms. Direct fully specified
transforms remained correct; only the clarification-resume path was broken.

## Root cause locus

| Stage | Finding |
|-------|---------|
| Pending save (pre-fix) | Stored free-text `goal` only — no typed draft |
| Follow-up merge | Combined text; no field-level merge |
| Intent reconstruction | `_try_clarified_artifact_action` → `_artifact_spec_from_clarification_resume` |
| Tool invocation | `documents.generate` with source basename as `output_name` |
| Substitution point | **`_artifact_spec_from_clarification_resume` name/format extraction** |

## Fix

1. **Typed pending transform draft** on incomplete transform clarification:
   - `intent_kind = TRANSFORM_EXISTING_DOCUMENT`
   - exact source identity, known format/destination, missing fields
   - conversation ID, collision policy, allowed root
2. **Follow-up fills only missing draft fields**; source never becomes destination.
3. Complete draft → same sealed **`documents.convert`** executor as RB-5
   (`_run_typed_artifact_intent`).
4. Zero generic arbiter / mission / free tool-loop on this path.
5. Partial follow-up → one next clarification, zero side effects.
6. Conversation-scoped draft keys; cross-conversation isolation.
7. Pending closed only after **verified** exact-path success (RB-4).
8. Repeat follow-up after `status=completed` → no second convert.
9. Safety: `_artifact_spec_from_clarification_resume` no longer treats source as dest.

## RB-4 / RB-5 preservation

- Exact destination verification (`_verified_artifact_answer`) unchanged in contract.
- Fully specified direct transform still sealed before arbiter/mission.
- RB-4 and RB-5 tests remain green (included in validation).

## Live-style acceptance (isolated runtime home, LLM disabled)

| Gate | Result |
|------|--------|
| Clarification follow-up | **6/6** exact dest, convert only, source hash stable |
| Direct fully specified transform | **12/12** |
| Incomplete transform (pre-answer) | **6/6** clarification, zero files |
| Two conversations | **3/3** isolated source/dest |
| Retry/reload | **3/3** zero duplicate artifact |
| Existing recall | **3/3** |
| Direct NEW_ARTIFACT | **3/3** |
| Internal envelope blocked | **3/3** |
| LLM calls on sealed paths | **0** |

## Validation

| Check | Result |
|-------|--------|
| `test_rb6_transform_followup.py` | 14 passed |
| `test_rb5` + `test_rb4` + rb6 | 41 passed |
| Full backend suite | **918 passed, 13 skipped** |
| Ruff 0.8.4 | All checks passed |
| `compileall backend/src qa` | exit 0 |
| `git diff --check` | clean |
| Secret scan | 0 hits |

## Out of scope / not done

- No push, no merge, no attestation, no READY.
- No main / old candidate / `.audit/**` mutation.
- No production state used.
- Fail-closed disable path **not** taken (gates achieved).
