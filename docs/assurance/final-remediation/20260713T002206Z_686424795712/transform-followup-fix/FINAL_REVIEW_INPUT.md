# FINAL_REVIEW_INPUT — clarified transform continuation (RB-6)

## Candidate

| Field | Value |
|-------|-------|
| Status | `FINAL_TRANSFORM_FOLLOWUP_CANDIDATE_FOR_REVIEW` |
| RUN_ID | `20260713T002206Z_686424795712` |
| Base | `d2372de0e7c3c5e6d3c3314f3ec489e618474946` |
| Branch | `fix/final-transform-followup/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-transform-followup-fix-20260713T002206Z_686424795712` |
| Fix commit | `956411609f04977ce2625942415b383095f98aa8` |
| Assurance commit | branch tip (`assurance: record clarified transform continuation`) |

## What to review

**Only RB-6**: clarification follow-up for transform requests must restore a typed
`TRANSFORM_EXISTING_DOCUMENT` contract and execute the sealed convert path.

Do **not** re-litigate RB-4 (exact destination verification) or RB-5 (direct fully
specified transform routing) except to confirm they remain green.

## Contract under review

1. Incomplete transform → one clarification + typed pending draft (source bound,
   format/destination missing).
2. Follow-up supplies only missing fields; source never becomes destination.
3. Complete draft → `documents.convert` via `_run_typed_artifact_intent` (same as RB-5).
4. Zero arbiter / mission / free tool-loop on clarified transform resume.
5. Final success path only from verified exact result (RB-4).
6. Conversation isolation; retry/reload no duplicate; completed re-follow-up no second convert.
7. Mismatch `actual_path != requested_destination` → failure, no success claim.

## Primary evidence

- `docs/assurance/final-remediation/20260713T002206Z_686424795712/transform-followup-fix/REMEDIATION_SUMMARY.md`
- `VALIDATION.md` (suite, ruff, live-style matrix)
- Tests: `backend/tests/test_rb6_transform_followup.py` (A–M)
- Production: `backend/src/jarvis_gpt/agent.py` (pending draft + clarified resume)

## Reviewer gates (minimum)

| Gate | Expected |
|------|----------|
| Clarified transform | 6/6 exact requested artifact |
| Direct transform | 12/12 still sealed |
| Isolation / retry | 3/3 each |
| False success on source name | 0 |
| Backend suite | green |
| Push/merge/attestation/READY | none present |

## Explicit non-goals

- Do not push or merge.
- Do not create attestation or READY from this package.
- Do not modify main, older candidates, or `.audit/**`.
