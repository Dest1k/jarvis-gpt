# Release blocker remediation

| Field | Value |
|-------|-------|
| RUN_ID | `20260713T002206Z_686424795712` |
| Base final candidate | `8aa2823ce40a8ed41555a8b1f9ec89de59deaad3` |
| Branch | `fix/final-release-blockers/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-release-blockers-20260713T002206Z_686424795712` |
| Status | **FINAL_RELEASE_BLOCKER_REMEDIATION_CANDIDATE_FOR_REVIEW** |
| Head | `10787388c2ceddce9c219e1181b7ba82ecd4e316` |
| Independent attestation | **not claimed** |
| Product READY | **not claimed** |
| Push/merge | **none** |

## Claude release blockers addressed

### RB-1 — pinned Ruff / CI / doctor

- Pinned toolchain: `ruff==0.8.4` (`backend/requirements-dev.txt`).
- Exact command: `py -3.11 -m ruff check --no-cache backend/src backend/tests`
- Base (`fc19886`): **1** error (pre-existing `agent.py` SIM103).
- Candidate `8aa2823`: **17** errors (16 candidate-introduced + 1 pre-existing).
- After fix: **0** errors, exit 0.
- Pre-existing SIM103 fixed as release hygiene (marked in code comment).
- CI step `Lint backend` and doctor/smoke `backend lint` share the same pinned contract (regression `test_doctor_and_ci_share_pinned_ruff_lint_contract`).
- Commit: `534cf2e57dc47ad779f18e03045eba978c0f3b35` — `fix: restore pinned lint and doctor gate`

### RB-2 — real clarification before side effects

- Claude live on base candidate: **1/6** clarification; **4/6** wrote artifacts without asking (artifact half failed).
- Old SPARK-0005 commit was **test-only** (FailLLM / `JARVIS_LLM_ENABLED=0`) — **offline test gap**, not live acceptance.
- Fix: completeness-based side-effect admission **before** mission/artifact/mutating tools; second-line tool gate; conversation-local pending clarification; deterministic artifact resume after operator answer (prevents shopping/DNS hijack).
- Offline regressions: A–F in `backend/tests/test_agent.py` (model-shaped generate blocked, zero mutation, follow-up resume, unambiguous skip, isolation, retry).
- Live API (Claude scenario prompt): **6/6** one question, zero files/missions before answer (recorded earlier on running turbo stack; re-verified after resume path).
- Follow-up resume: deterministic generate after answer; offline **PASS** with real `documents.generate`; live API re-check depends on stack uptime (see VALIDATION.md).
- Commit: `10787388c2ceddce9c219e1181b7ba82ecd4e316` — `fix: require clarification before artifact side effects`

## Explicit non-claims

- The original SPARK-0005 patch alone did **not** pass live acceptance.
- This package does **not** create independent attestation or product READY.
- Old final-remediation worktree/branch left unchanged at `8aa2823ce40a8ed41555a8b1f9ec89de59deaad3`.

## Residual gaps

See VALIDATION.md / RESIDUAL_GAPS updates (doctor full-suite timeout P2, live stack flakiness of Docker Desktop on host, etc.).
