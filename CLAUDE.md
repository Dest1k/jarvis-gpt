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

## Documents & spreadsheets

- DOCX and XLSX are generated as **hand-rolled OpenXML** in
  `backend/src/jarvis_gpt/document_runtime.py` — there is intentionally no
  `python-docx`/`openpyxl` runtime dependency, so generation works offline with no
  install step. `write_markdown_docx` renders Markdown (heading styles, inline
  bold/italic/`code`/links, bullet & numbered lists, bordered tables with a shaded
  header). `write_workbook_xlsx` builds real spreadsheets (typed cells, `=formulas`,
  bold frozen auto-filtered header); `build_workbook_sheets` accepts a structured
  `sheets` arg or parses Markdown tables / CSV. Both are exposed through
  `documents.generate` (xlsx via `output_format="xlsx"` + optional `sheets`).
- When changing the OpenXML writers, validate the output with a real Office parser
  (openpyxl / python-docx via `uv run --with ...`) **and** round-trip through the
  project's own `extract_document` — malformed XML still writes a file but won't open
  in Office. Tests live in `backend/tests/test_document_generation.py`.

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

## Hybrid brain (scaffold — INACTIVE)

- The active brain is the local Gemma profile. `backend/src/jarvis_gpt/frontier_brain.py`
  is a **prepared-but-off** second brain that can delegate hard reasoning/synthesis to a
  frontier model. It is dormant: `build_frontier_brain` returns `None` and `select_brain`
  returns `"local"` unless the owner opts in, so `LLMRouter.frontier` is `None` and nothing
  delegates. **Do not route through it or invoke it until the owner asks.**
- By owner requirement it reaches the frontier model through the **logged-in Claude Code
  CLI** (their subscription), **not** a billed API key: `claude -p <prompt> --model
  claude-opus-4-8 --effort medium --output-format text`. The fixed target is **Opus 4.8 at
  medium effort**.
- Activate later with `JARVIS_ENABLE_HYBRID_BRAIN=1` (optional overrides:
  `JARVIS_FRONTIER_MODEL`, `JARVIS_FRONTIER_EFFORT`, `JARVIS_FRONTIER_CLI`,
  `JARVIS_FRONTIER_TIMEOUT_SEC`). `select_brain()` is the single flip-point to wire agent
  hard-reasoning calls through. Tests: `backend/tests/test_frontier_brain.py`.
