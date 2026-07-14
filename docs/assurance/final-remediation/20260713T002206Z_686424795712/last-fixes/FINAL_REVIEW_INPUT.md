# FINAL_REVIEW_INPUT — last-fixes candidate

## Verdict request

Please re-review **only** RB-1-R and RB-3 on this candidate. Do not expand scope to a full new wave.

## Candidate identity

| Field | Value |
|-------|-------|
| RUN_ID | `20260713T002206Z_686424795712` |
| Base | `27e364c88f657cd4205360186a926d57e9b21c5c` |
| Branch | `fix/final-release-last-fixes/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-release-last-fixes-20260713T002206Z_686424795712` |
| Status claimed by implementer | `FINAL_RELEASE_LAST_FIXES_CANDIDATE_FOR_REVIEW` |
| Attestation / READY / push | **none** |

## Commits to inspect

1. `751b2e4c4d14044ca936ba7e4604eb1e0caff08a` — doctor timeout resource-safe (RB-1-R)
2. `bd922cbd7d1eb156f4531efee19fa32e87d7e43c` — exact artifact path binding (RB-3)
3. assurance commit recording evidence (docs only)

## Acceptance already measured

- Clean doctor: exit 0 / ok=true / required failures=0 / timeout default 600 s
- Forced invalid timeout: nonzero / ok=false
- Live exact artifact: 3/3
- Ambiguous clarification: 6/6
- Follow-up artifact: 3/3
- Backend suite 877 passed; QA 218 passed; ruff 0.8.4 clean

## Primary files

- `scripts/smoke.py`, `backend/tests/test_smoke_script.py`
- `backend/src/jarvis_gpt/agent.py`
- `backend/src/jarvis_gpt/tools.py`
- `backend/src/jarvis_gpt/document_runtime.py`
- `backend/tests/test_agent.py`, `backend/tests/test_document_runtime.py`

## Residual gaps (non-blocking)

See `REMEDIATION_SUMMARY.md` residual section. Host-bridge flake under load; dispatcher flakiness; upload 405 for recall seed.

## Reviewer instruction

Confirm or reject **FINAL_RELEASE_LAST_FIXES_CANDIDATE_FOR_REVIEW**. Do not create READY or independent attestation in this step unless product gate explicitly requires a separate attestation wave.
