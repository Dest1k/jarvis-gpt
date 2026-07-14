# SPARK-0009 — Canonical approval action (scope-remediated)

- Status: `PASS`
- Finding: `FUNC-FIND-009`
- Runtime impact: `code_only`
- Pre-task HEAD: `cd5e05549613aea95c49f8cae6c2d9111f467e85`
- Remediation: Wave 0 scope fix — mkdir-only alias; no `execution_protocol.py`

## Finding

Approval execution referenced `filesystem.mkdir` while allowed action is `fs.mkdir`; no directory was created.

## Before

`ActionEnvelope` only accepted `kind: fs.mkdir`. Model alias `filesystem.mkdir` failed schema / left non-executable approvals.

## Patch (allowed files only)

1. `tools.py` — `_canonicalize_tool_invocation` rewrites **only** bare `filesystem.mkdir` → `execution.apply`/`fs.mkdir`; payload rewrite limited to `filesystem.mkdir` → `fs.mkdir`
2. `agent.py` / `approval_executor.py` — canonicalize before approval create/execute; unknown aliases rejected without pending approval
3. **Not changed:** `execution_protocol.py`
4. **Not added:** aliases for write/append/move/rename/copy/delete/remove

## After

- Approvals with `filesystem.mkdir` bind canonical `fs.mkdir` and create only the approved path once
- write/move/delete aliases do not canonicalize
- Unknown aliases rejected without pending-approval pollution
- Neighboring paths untouched

## Negative coverage

- mkdir alias canonicalize
- write/move/delete aliases do not canonicalize
- unknown alias does not create approval
- exact approved directory created once
- neighboring paths not affected
- `execution_protocol.py` absent from changed paths

## Commands

```text
py -3.11 -B -m pytest backend/tests/test_approval_executor.py backend/tests/test_tools.py backend/tests/test_agent.py -q
```

## Scope

- `backend/src/jarvis_gpt/tools.py`
- `backend/src/jarvis_gpt/agent.py`
- `backend/src/jarvis_gpt/approval_executor.py`
- `backend/tests/test_tools.py`
- `backend/tests/test_approval_executor.py`
- `backend/tests/test_agent.py`
- execution report/manifest under `docs/assurance/remediation/**`
