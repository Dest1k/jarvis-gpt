# SPARK-0006 — Internal tool-envelope containment

- Status: `PASS`
- Finding: `FUNC-FIND-006`
- Runtime impact: `code_only`
- Pre-task HEAD: `e276df67deb9cf77b4b42a84a17c6ea51bcb66a1`
- Checkpoint tag: `pre-functional-20260713T002206Z_686424795712-SPARK-0006`

## Finding

Raw `call:documents`, `call:llm.health`, `call:dispatcher.status`, or JSON tool payloads were rendered in stream/final/DOM.

## Before (FAIL reproduction)

```text
_classify_tool_turn('call:documents.read') -> answer  # LEAK
_classify_tool_turn('call:llm.health') -> answer       # LEAK
_classify_tool_turn('call:dispatcher.status') -> answer
# mixed call:+JSON was already protocol_error
```

## Patch

In `backend/src/jarvis_gpt/agent.py`:

- Detect bare `call:` markers and broader tool envelopes
- `_contains_internal_tool_output` / `_user_visible_answer` gate finals
- `_classify_tool_turn` treats call markers / tool_calls as protocol_error (never answer)
- Stream and non-stream final assembly use `_user_visible_answer`

Regression tests in `test_llm.py`, `test_agent.py`, `test_api_smoke.py`.

## After

- Marker samples → `protocol_error` or `tool`, never user-visible `call:` / raw JSON
- Stream deltas + terminal answer for envelope payloads = `TOOL_PROTOCOL_FAILURE_ANSWER` without markers

## Commands

```text
py -3.11 -B -m pytest backend/tests/test_llm.py backend/tests/test_agent.py backend/tests/test_api_smoke.py -q
→ 145 passed

py -3.11 -B -m qa.cli validate-suite qa\suites\operator_core
→ ok
```

## Scope

- `backend/src/jarvis_gpt/agent.py`
- `backend/tests/test_agent.py`
- `backend/tests/test_llm.py`
- `backend/tests/test_api_smoke.py`
- execution report + MANIFEST

## Residual risks

- No live OP-0025.. GUI journey in this task (fixture/stream unit coverage of exact markers)
- Frontend not changed; backend now never emits envelopes on stream/final
