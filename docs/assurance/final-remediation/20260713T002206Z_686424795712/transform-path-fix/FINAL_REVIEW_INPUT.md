# FINAL_REVIEW_INPUT — transform-path (RB-4)

## Candidate

| Field | Value |
|-------|-------|
| Status | **FINAL_TRANSFORM_PATH_CANDIDATE_FOR_REVIEW** |
| RUN_ID | `20260713T002206Z_686424795712` |
| Base | `38f606fcef9e7947c14b62a9a815da6445f4196e` |
| Production fix | `18f15c0dcb96d096a0e484d0d5a22f78e424c6ff` |
| Branch | `fix/final-transform-path/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-transform-fix-20260713T002206Z_686424795712` |
| Claude blocked report | `D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-last-rereview-claude\FINAL_LAST_REVIEW_BLOCKED.md` |

## What to review

Only **RB-4**: `TRANSFORM_EXISTING_DOCUMENT` must bind the exact requested
destination before tool execution and may declare success only when that exact
verified path exists (not the source, not a format subdirectory, not a
timestamp fallback).

## Suggested independent checks

1. Read the typed contract in `agent.py` (`_new_artifact_intent_from_message`,
   `_try_direct_new_artifact_action`, `_verified_artifact_answer`).
2. Read `documents.convert` success fields and exact-path gate in `tools.py`.
3. Run `pytest backend/tests/test_rb4_transform_path.py -q`.
4. Live: upload/seed a source document under a temporary `JARVIS_HOME`, issue six
   transform requests with explicit destinations; confirm 6/6 exact files and
   unchanged source hash.
5. Negative: force a tool result path that is not the requested destination;
   confirm failure and zero success claim.
6. Confirm recall and new-artifact routes still pass (non-regression).

## Must remain true

- No push/merge performed by implementer.
- `main` unchanged.
- No attestation / READY document.
- Frontend tree unchanged (`bf6cb7ce…`).
- `.audit/**` and prior candidates/reviews not modified.

## Prior accepted work not reopened

- RB-1-R doctor timeouts
- RB-3 direct artifact / clarification / follow-up paths (still green; only
  transform half was the open blocker)

## Deliverable pointer

- `REMEDIATION_SUMMARY.md`
- `STATE.json`
- `COMMIT_MAP.csv`
- `VALIDATION.md`
- Live evidence under audit-backups `final-transform-fix/live-results/`
