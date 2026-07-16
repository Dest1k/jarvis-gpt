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

- An explicit "do X / open X" command in the operator's current turn is treated
  as authorization only when the tool-specific matcher proves the exact operands;
  that exact deterministic action may then run without another approval.
  The deprecated `JARVIS_OPERATOR_FULL_AUTONOMY` setting is a no-op retained for
  configuration compatibility. See `backend/src/jarvis_gpt/config.py` and the
  operator-authority logic in
  `backend/src/jarvis_gpt/agent.py` (`_operator_action_scopes`,
  `_operator_tool_arguments_match`, `_operator_requested_tool_names`).
- Scope only selects candidate tools; it never authorizes model-selected paths,
  URLs, payloads, or arguments. `allow_review_tools` / `allow_danger_tools` expose
  tools for proposal but do not execute them, and `approval_required_for` remains
  gated inside the model-driven agentic loop. The loop budget is bounded to
  1..24 steps.
