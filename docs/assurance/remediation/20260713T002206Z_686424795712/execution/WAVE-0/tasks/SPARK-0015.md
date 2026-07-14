# SPARK-0015 вЂ” Runtime-home transcript isolation

- Status: `PASS`
- Finding: `FUNC-FIND-015`
- Runtime impact: `code_only` (browser localStorage keying)
- Pre-task HEAD: `7a014ac9c5894ac3a72954d6c8858ef0f25c51d3`
- Checkpoint tag: `pre-functional-20260713T002206Z_686424795712-SPARK-0015`

## Finding

GUI retained prior transcript while backend conversation count was zero after home switch.

## Patch

- Scope chat windows/settings localStorage by `home::profile` runtime identity
- Load scoped storage only after `/api/status` identity is known
- On identity change, replace client transcript from that identity's store (empty for new home)
- Do not read unscoped legacy keys once identity is known

## Commands

```text
npm run test:runtime-identity в†’ runtime-identity-ok
npx tsc --noEmit в†’ 0
pytest test_api_smoke.py::test_empty_home_reports_zero_conversations в†’ passed
```

## Scope

- `frontend/app/page.tsx`
- `frontend/package.json`
- `backend/tests/test_api_smoke.py`
- execution report

