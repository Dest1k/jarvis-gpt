# CLAUDE.md

Guidance for AI coding agents working in this repository.

## Git workflow

- **Always commit and push directly to `main`.** The owner wants changes to
  land on `main` without per-change confirmation and without spinning up a
  separate feature branch — unless they explicitly ask for a branch or a pull
  request in a given task.
- Keep history linear: prefer fast-forward / rebase over merge commits.
- Only open a pull request when the owner explicitly asks for one.

## Project layout

- `backend/` — Python service (`jarvis_gpt` package), managed with `uv`.
- `frontend/` — Next.js app.

## Backend dev commands

Run from `backend/`:

- Install deps: `uv sync --frozen`
- Run tests: `uv run --with pytest python -m pytest tests/ -q`
- Lint: `uv run --with ruff==0.8.4 ruff check src tests` (line length 100)

Note: some tests exercise Windows/PowerShell host-bridge scripts and fail on
Linux CI hosts regardless of code changes — diff the failing-test set before
and after a change rather than assuming a red test is a new regression.

## Operator permissions / autonomy

- The runtime ships in **owner full-autonomy** mode: `JARVIS_OPERATOR_FULL_AUTONOMY`
  defaults on (`backend/src/jarvis_gpt/config.py`). The single operator is the system
  administrator, so their own chat turn authorizes the work it asks for. In this mode
  the runtime never stops to ask a clarifying question and never mints an approval
  gate before acting; the model sees the complete toolset and its chosen tools run
  through the verified operator-authorization path. The chat is kept to
  request → analysis → action → result: internal reasoning, memory bookkeeping,
  approval prompts and clarify/blocked routes stay in the audit event log but do not
  stream to the UI. See `agent.py` `_owner_autonomy_active`, `_admit_side_effects`,
  `_tools_for_context`, `_run_agentic_tool` (autonomy grant), and `_suppress_from_chat`.
- Set `JARVIS_OPERATOR_FULL_AUTONOMY=0` for the **gated** posture: clarify-first for
  under-specified deliverables, approval gates for review/danger and
  `approval_required_for` tools, and per-tool exact-operand matching
  (`_operator_action_scopes`, `_operator_tool_arguments_match`,
  `_operator_requested_tool_names`). The test suite pins this posture via
  `backend/tests/conftest.py`; `test_owner_autonomy.py` covers the autonomous default.
- Reliability guarantees hold in **both** modes and are correctness, not gates: atomic
  operator-effect keys and duplicate suppression, executive action contracts, and
  verified writes. The agentic loop budget is bounded to 1..24 steps.
- Incoming requests are folded through `_fold_operator_confusables` before intent
  detection (zero-width/non-breaking spaces, `ё`→`е`, NFC) so copy-paste and phrasing
  quirks do not defeat command recognition.
