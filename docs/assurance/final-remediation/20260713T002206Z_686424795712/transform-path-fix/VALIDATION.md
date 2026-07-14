# VALIDATION — final transform-path remediation (RB-4)

## 1. Isolation

| Check | Result |
|-------|--------|
| Worktree | `D:\jarvis-gpt-worktrees\final-transform-fix-20260713T002206Z_686424795712` |
| Branch | `fix/final-transform-path/20260713T002206Z_686424795712` |
| Base | `38f606fcef9e7947c14b62a9a815da6445f4196e` |
| HEAD == BASE before edits | **True** |
| Rollback bundle | `D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-transform-fix\rollback.bundle` |

## 2. Reproduction (pre-fix classification evidence)

Offline intent extraction on Claude-style prompts showed:

| Prompt family | Pre-fix defect |
|---------------|-----------------|
| `Преобразуй … в markdown … как X.md` | destination = source-doc.txt; rel_dir = `markdown/` |
| `Transform the uploaded document …` | misclassified as EXISTING_DOCUMENT_REFERENCE |
| `Convert uploaded … write Y.md` | kind None / source filename preferred |
| Live Claude table | 0/6 requested outputs; success for wrong path |

Post-fix intent extraction for the same families:

- kind = `TRANSFORM_EXISTING_DOCUMENT`
- source and destination are distinct fields
- `rel_dir` never becomes a format label
- `requested_destination` = `document-outputs/<exact-file>`

## 3. Regression tests A–L

File: `backend/tests/test_rb4_transform_path.py` — **14 passed**

| ID | Case | Result |
|----|------|--------|
| A | Transform exact path | PASS |
| B | Tool returns other existing path → FAIL | PASS |
| C | Tool returns source as output → FAIL | PASS |
| D | Directory-like destination → error, no side effects | PASS |
| E | Format label subdirectory rejected at intent | PASS |
| F | Timestamp fallback → FAIL | PASS |
| G | Collision without overwrite → FAIL, hash unchanged | PASS |
| H | Source hash unchanged | PASS |
| I | Final response cannot invent path | PASS |
| J | Existing-document reference stays recall | PASS |
| K | NEW_ARTIFACT_REQUEST stays direct generation | PASS |
| L | Two conversations do not mix contracts | PASS |
| + | Source/destination never swap | PASS |
| + | Path outside allowed root rejected | PASS |

Adjacent RB-3 / document runtime tests: **PASS** (42 focused tests including RB-3/document/clarify).

## 4. Live acceptance (temporary allowed root)

Runtime home under audit-backups (not production user state).

Evidence JSON:
`D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-transform-fix\live-results\live-acceptance.json`

| Gate | Required | Measured | Verdict |
|------|----------|----------|---------|
| Transform exact destination | 6/6 | 6/6 | **PASS** |
| Negative mismatched-tool-path | 3/3 failure, no success claim | 3/3 | **PASS** |
| Existing document recall | 3/3 | 3/3 | **PASS** |
| Direct new artifact exact path | 3/3 | 3/3 | **PASS** |
| Ambiguous clarification | 3/3 no side effects | 3/3 | **PASS** |
| Follow-up artifact | 3/3 | 3/3 | **PASS** |
| Source hash unchanged across transforms | required | True | **PASS** |
| Timestamp fallback | zero | zero | **PASS** |
| Invented markdown subdirectory | zero | zero | **PASS** |

Deterministic agent path (`JARVIS_LLM_ENABLED=0`) exercises the exact pre-tool
binding contract that RB-4 requires. Complete transform requests no longer depend
on LLM argument invention for destination binding.

## 5. Full suites

| Check | Result |
|-------|--------|
| pinned ruff 0.8.4 `backend/src backend/tests` | **All checks passed, exit 0** |
| `compileall backend/src qa` | **exit 0** |
| Full backend pytest | **891 passed, 13 skipped**, ~174 s, exit 0 |
| QA pytest | **218 passed, 3 skipped**, exit 0 |
| `qa.cli validate-suite operator_core` | ok, 1 scenario |
| `qa.cli validate-evidence` | ok, 8 records |
| `qa.cli replay` | ok, 8 cases (1 PASS · 6 FAIL · 1 INCONCLUSIVE), 0 mismatches |
| `git diff --check` | clean |
| Secret scan over production diff | **0 hits** |
| Frontend tree | `bf6cb7cefff8ccea7edab7952e7c476980647567` — **equal** to accepted evidence; no frontend edits |

## 6. Out of scope (not re-run)

- Full doctor journey (RB-1-R already accepted; smoke/doctor trees untouched)
- Frontend npm build (frontend tree byte-identical; not modified)
- 31B models (forbidden by task)
- Production user documents / production state
- Push / merge / attestation / READY

## 7. Candidate status

```text
FINAL_TRANSFORM_PATH_CANDIDATE_FOR_REVIEW
```
