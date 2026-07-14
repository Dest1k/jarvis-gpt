# Validation — RB-6 clarified transform continuation

RUN_ID: `20260713T002206Z_686424795712`  
BASE: `d2372de0e7c3c5e6d3c3314f3ec489e618474946`  
FIX: `956411609f04977ce2625942415b383095f98aa8`

## 1. Isolation

- New worktree created from exact base.
- `git rev-parse HEAD` at creation == `d2372de0e7c3c5e6d3c3314f3ec489e618474946` (**HEAD_EQUALITY=PASS**).
- Rollback bundle: `D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-transform-followup-fix\`.
- No production state; isolated `JARVIS_HOME` for all acceptance runs.
- No push / merge / main mutation / `.audit/**` edits.

## 2. Pre-fix reproduction (offline, base logic)

Combined incomplete transform + follow-up through pre-fix helpers:

| Field | Observed (broken) |
|-------|-------------------|
| original request | `На основе загруженного документа srcX-c08236.txt подготовь отчёт в нужном формате и положи куда следует.` |
| gaps on original | `['destination', 'format']` |
| follow-up | `Формат markdown, имя файла wantX-211c7.md, каталог document-outputs.` |
| `_artifact_spec_from_clarification_resume` | `output_name=srcX-c08236.txt`, `output_format=txt` |
| substitution locus | clarification resume name/format extraction (first filename / source `.txt`) |
| requested dest | never bound |
| tool path | would be `documents.generate` with source basename |

Repeated structural probe (6 scenarios with unique source/dest names) confirmed the same substitution class before the fix.

## 3. Focused tests

```text
pytest backend/tests/test_rb6_transform_followup.py \
       backend/tests/test_rb5_transform_route.py \
       backend/tests/test_rb4_transform_path.py -q
→ 41 passed

pytest backend/tests/test_agent.py -k "clarif or artifact or transform or document or pending or recall" -q
→ 14 passed, 110 deselected
```

RB-6 coverage matrix (A–M): typed draft, follow-up restore, no source-as-dest,
deterministic convert, exact path, partial follow-up, conversation isolation,
retry/reload, no second transform, RB-5 direct sealed, recall, envelope, tool mismatch.

## 4. Full backend suite

```text
pytest backend/tests -q
→ 918 passed, 13 skipped in ~175 s (exit 0)
```

## 5. Tooling gates

| Gate | Command / scope | Result |
|------|-----------------|--------|
| Ruff 0.8.4 | `ruff check backend/src backend/tests` | All checks passed |
| compileall | `python -m compileall -q backend/src qa` | exit 0 |
| whitespace | `git diff --check` | clean |
| secrets | diff pattern scan | 0 hits |

Frontend / doctor / QA not re-run (tree equality vs base for those areas; only
`agent.py` + new test + assurance docs changed).

## 6. Live-style acceptance (isolated turbo-compatible path)

Runtime: fresh `JARVIS_HOME`, `JARVIS_LLM_ENABLED=0`, seeded source files,
deterministic sealed routes (no model).

| Scenario | Required | Measured |
|----------|----------|----------|
| Clarification transform follow-up | 6/6 | **6/6** — exact dest, convert only, no source-name file, source hash stable, zero mission |
| Direct fully specified transform | 12/12 | **12/12** |
| Incomplete transform pre-answer | 6/6 | **6/6** — clarification, zero new files |
| Two conversations | 3/3 | **3/3** |
| Retry/reload | 3/3 | **3/3** — single artifact |
| Existing recall | 3/3 | **3/3** |
| Direct NEW_ARTIFACT | 3/3 | **3/3** |
| Internal envelope blocked | 3/3 | **3/3** |
| LLM calls on sealed paths | 0 | **0** |

## 7. Stop-rule gates

| Gate | Status |
|------|--------|
| clarified transform live 6/6 | **PASS** |
| direct transform live 12/12 | **PASS** |
| conversation isolation 3/3 | **PASS** |
| retry/reload 3/3 | **PASS** |
| zero false success | **PASS** |

Fail-closed disable path not activated.
