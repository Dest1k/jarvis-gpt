# Assistant Notes

Single coordination point for assistant-to-assistant handoffs in this repository.

Use this file for short, append-only notes between Codex and the second assistant.
Keep the newest note at the top, include author, date, branch/commit when useful,
and list only facts needed by the next assistant: changed files, tests, blockers,
and decisions. Do not paste secrets, tokens, private logs, or long command output.

## Notes

### 2026-07-09 - Codex

- Reviewed Claude's recent trend: replace keyword plugs with reasoning-first
  routing, give the model safe read-only tools, verify outputs, and close the
  feedback/lesson loop.
- Found and fixed a matching approval-boundary weakness: public
  `POST /api/tools/{tool_name}/run` no longer honors client-supplied
  `allow_danger=True`. Direct API tool runs are safe-only; review/danger tools
  must go through approval creation and `ApprovalExecutor`.
- Tightened CORS from wildcard to loopback origins by default, with optional
  `JARVIS_CORS_ORIGINS` for explicit extra trusted origins. This reduces browser
  drive-by access to the local API/approval surface.
- Added smoke coverage so the API refuses `host.bridge.execute` even when a
  client tries `allow_danger: true`, and so non-loopback CORS preflight fails.

### 2026-07-09 - Claude (arbiter owns local_action)

- Follow-up to system.inspect: the tool gave the model hands, but the ROUTE was
  still keyword-decided. Now the reasoning-first arbiter owns local tasks too.
- Two gaps closed in agent.py:
  1. `arbiter.route == "local_action"` was produced but ignored (fell through to
     web branches). `_try_direct_action` now handles it: reroute via new
     `_local_action_plan_from_intent` and return None → agentic loop with native
     tools. Stops "покажи автозагрузку" being web-searched.
  2. The arbiter only ran for web_research plans; plain machine questions land in
     reasoning/local_admin_advice and never reached it. `_understand_intent`'s
     gate now also opens for that local bucket. Narrowly scoped (only
     local_admin_advice), offline unchanged (gated on llm_enabled), one extra
     router call only in that already-local bucket.
- Arbiter prompt strengthened: local_action now explicitly covers reading machine
  state (hardware/OS/disks/RAM/battery/services/startup/printers/network/processes)
  AND actions (open app, type, focus window, local command), with examples and
  "this is NOT web_research".
- Deterministic native fast-paths untouched (they return before the arbiter), so
  all offline native tests are unchanged.
- Tests: test_arbiter_routes_local_query_to_native_inspection,
  test_arbiter_gate_opens_for_local_bucket_and_stays_closed_for_chat. 190 pass,
  ruff clean, frontend clean.

### 2026-07-09 - Claude (system.inspect: unlock WMI understanding)

- Diagnosis for "why doesn't a 26-31B model get everyday Windows requests": the
  model's WMI/WinAPI knowledge is fine, but the native route is decided by
  keyword heuristics before the model, and the read-only WMI path was fused into
  the danger-level windows.native tool — so the agentic loop could never use it.
  The 5-class keyword map only fired on the literal word "wmi"/"cim".
- Added safe read-only tool `system.inspect` (danger_level=safe) with an action
  allowlist `SAFE_INSPECT_ACTIONS` = {capabilities, window.list, wmi.query};
  mutating actions are refused with a pointer to approval-gated windows.native.
  Shares `_run_native_bridge_command` with windows.native; reuses the already
  validated wmi.query payload (SELECT-only, alnum class/props, no methods).
- Because it is safe and not in AGENTIC_TOOL_DENYLIST, it is auto-listed in the
  agentic tool protocol every turn; the model picks the Win32_* class itself for
  any everyday machine-state question. Heuristics stay as the offline fallback.
  SYSTEM_PROMPT nudges toward it and says not to wait for the word "wmi".
- Tests (4): read-only wmi run, mutating-action refusal, safe-autonomous
  membership, and an agentic test where the model calls system.inspect on
  "сколько заряда осталось на ноуте?" with no "wmi" keyword. 188 pass, ruff
  clean, frontend clean.
- Future candidates: run the whole local_action route through the reasoning-first
  arbiter (as web_research already does); a broader safe WinAPI read tool.

### 2026-07-09 - Claude (hardening pass)

- Audit of critical paths after three feature layers; fixed the real gaps,
  confirmed SQLite concurrency (single conn + RLock, check_same_thread=False)
  and web.fetch body cap were already sound.
- `backend/tests/test_api_smoke.py`: first end-to-end test through the real
  ASGI app (offline LLM, autonomy off) — status/chat/feedback/mission/report/
  queue/memory/tools/approvals/persona, incl. 404/422 and danger-tool refusal.
  Boot via `with TestClient(app)` runs the lifespan. It caught two wrong
  contract assumptions immediately (that is the point).
- Verify/repair now run under `asyncio.wait_for(self._verify_timeout())`
  (`VERIFY_TIMEOUT_SEC=45`, capped by llm_timeout): a hung critic degrades to
  shipping the ready draft instead of blocking for the full LLM timeout.
- `.env.example` synced with config (verify/embeddings/mission-interval vars).
- Known residual, deliberately not changed: web.fetch SSRF pre-flight is a
  DNS-rebinding TOCTOU. Realistic attacks are already blocked; a full IP pin
  breaks TLS SNI for legitimate HTTPS — worse than the rare rebinding for a
  single-operator local tool. Revisit with a custom httpx transport if a
  multi-tenant scenario appears.
- Full run: 184 pass, ruff clean, frontend typecheck + build clean.

### 2026-07-09 - Claude (experience loop)

- Closed the open half of the self-learning thesis: signals -> lessons ->
  behavior is now a loop, not a shelf.
- Operator feedback: `POST /api/messages/{id}/feedback` +
  `storage.set_message_feedback` (message metadata for UI restore, journal
  `operator.feedback` that survives chat deletion, audit, WS `feedback` event).
  Command Center has 👍/👎 on assistant bubbles (comment prompt on 👎).
- `verification.revise` verdicts are journaled from `_verify_and_repair_answer`.
- LearningEngine v2 derives priority lessons from negative/positive feedback,
  recurring self-check gaps, and rejected approvals — quoting real operator
  text; lesson cap raised to 6.
- `AgentRuntime._lessons_prompt()` injects top lessons (importance/recency,
  ~900 chars) into every chat/stream turn and mission step — this is the piece
  that makes learning change behavior deterministically.
- `answer_quality_report` + operator queue `quality` items
  (`quality:feedback` high, `quality:self-check` at >=3 revises).
- Frontend: feedback buttons, verification shield badge on bubbles (restored
  from metadata on reload), «Отчёт» button on done missions, auto-report after
  «Запустить всё». New CSS: `.bubbleAction.selected`, `.bubbleBadge`.
- Tests: `backend/tests/test_experience_loop.py` (5). Full run: 178 pass, ruff
  clean, frontend typecheck + build clean.
- Possible next steps: show quality history chart; let learning tick distill
  lessons via LLM (deterministic templates stay the fallback); feedback-driven
  persona insights.

### 2026-07-09 - Claude (result integrity layer)

- New module `backend/src/jarvis_gpt/verification.py`: strict JSON critic
  (`answer-verification-v1`), verdict parser, repair prompts (rewrite /
  stream addendum), deterministic + LLM mission report.
- Answer self-check wired into `chat()` (full rewrite allowed), `stream_chat()`
  (addendum only — streamed text is not retractable) and
  `_execute_mission_step_agentic` (report rewrite before notes persist).
  Trigger: tools used or answer >= 400 chars; one critic pass + max one repair.
  Kill switches: `JARVIS_VERIFY_ANSWERS=0` env or autonomy policy
  `verify_answers=false`. Unparseable critic output or JSON-shaped repair
  never damages the draft.
- Mission deliverable: `_maybe_finalize_mission` fires on the `done` transition
  from all three completion paths, is idempotent via KV `mission.report.{id}`,
  saves a `missions/report` memory, emits `mission_report`, and surfaces via
  `MissionRunResponse.final_report` + new `GET /api/missions/{id}/report`.
- Intent arbiter gained a `clarify` route: one targeted question to the
  operator (confidence >= 0.65) instead of a confident guess.
- Legacy loop-mechanics tests opt out via policy `verify_answers=false` so
  their LLM call counts stay about loop mechanics.
- Tests: `backend/tests/test_verification.py` (9). Full run: 173 pass, ruff
  clean, frontend typecheck + build clean.
- Possible next steps: Command Center panel for mission reports (API is ready);
  verification stats in operator queue; per-route verify policy.

### 2026-07-09 - Claude

- Closed three follow-ups previously marked "на будущее" in runtime.md, all on
  `main`:
  1. Persona auto-learning: added safe tools `persona.get` and `persona.insight`
     (tools.py) wired to `PersonaManager.add_insight`. `persona.insight` is
     deliberately allowed in the autonomous agentic loop (single fact, dedup,
     per-field caps, audit + `persona.insight` event) — the reasoning-first
     replacement for regex persona extraction. SYSTEM_PROMPT now tells the model
     to save durable operator facts sparingly.
  2. File-chunk hybrid retrieval no longer dies on zero lexical overlap:
     `storage.recent_file_chunks` provides a bounded fallback pool, gated by
     `FILE_FALLBACK_MIN_RELATEDNESS` (fuzzy-vector cosine >= 0.1) so unrelated
     files never leak into the prompt; fallback hits are marked
     `retrieval="semantic-recent"` and capped at 3.
  3. Mission detection through understanding: the intent arbiter's `mission`
     decision (confidence >= 0.7) now rewrites the kernel plan via
     `_mission_plan_from_intent`, and chat/stream re-read `context.task_plan`
     after `_try_direct_action`, so a mission-shaped task without mission
     keywords still becomes a persisted mission. Keyword counter stays as the
     offline path.
- Tests: `test_agentic_loop_learns_persona_insight_from_dialogue`,
  `test_persona_insight_tool_learns_deduplicates_and_validates`,
  `test_hybrid_files_falls_back_to_recent_chunks_without_lexical_overlap`,
  `test_reasoning_arbiter_can_promote_research_to_mission`.
- Full run before handoff: backend pytest 164 pass, ruff clean, frontend
  typecheck + build clean.
- Possible next steps: let the arbiter also own `local_action`; feed persona
  insights into the learning journal; persist chunk vectors for large corpora.

### 2026-07-09 - Codex

- Added `backend/src/jarvis_gpt/autonomy_executor.py`: a shared executor for
  persisted autonomy jobs, direct routine steps, and headless mission jobs.
- Supervisor now runs due background jobs on `JARVIS_AUTONOMY_MISSION_INTERVAL_SEC`
  while preserving existing approval gates. Mission jobs persist `mission_id`,
  stay enabled while budget remains, pause on blocked missions, and finish on done.
- The LLM now receives a compact capability/current-work manifest in normal chat
  and mission execution prompts: profile/model, current conversation/mission/task,
  safe autonomous tools, gated tools, recent missions, and background jobs.
- Command Center mission cards have `В фон`, which creates a persisted mission
  autonomy job instead of requiring the page to stay open.
- Tests run before handoff: `ruff`, full backend `pytest`, frontend `typecheck`,
  and frontend `build`.

### 2026-07-09 - Codex

- Added per-answer thought trace UI: assistant bubbles now show a Brain icon once
  the persisted `msg_*` id is known. It opens `/trace/{messageId}`.
- New backend endpoint `GET /api/agent/trace/message/{message_id}` returns the
  previous user input, assistant output, recorded runtime events, nodes/edges, and
  a disclosure that this is observable runtime trace rather than hidden CoT.
- Added a trace page with animated signal rail from input through task kernel,
  tools/memory/thought events, and output. It uses stored message metadata; no
  real browser opens or extra LLM calls are involved.

### 2026-07-09 - Codex

- Added an evidence-synthesis pass after `web.search`/`web.fetch`: web answers now
  ask the LLM to form a conclusion from fetched evidence, mark uncertainty, and
  keep source URLs, instead of returning only a mechanical source dump.
- Recent web evidence is stored per conversation under `research.last_web.*` and
  mirrored into `learning_observations` as `web.research`; follow-ups like
  "какой вывод?" reuse the saved evidence without opening the operator browser.
- The synthesis layer rejects router-shaped JSON or weak model output and falls
  back to the deterministic formatter, so offline/degraded behavior is preserved.
- While restarting the backend, found stale launcher-state PIDs can point at
  unrelated processes. `jarvis-launcher.ps1 stop/restart` now verifies the saved
  PID command line matches the expected Jarvis service before killing it.
- Regression tests cover successful synthesis, JSON fallback, and follow-up
  synthesis from previous evidence.

### 2026-07-09 - Codex

- Browser policy default is now `open`: validated public HTTP(S) browser opens no
  longer need approval. `browser.open`/`browser.open_many` are still excluded from
  the autonomous agentic tool loop, so background web work should use
  `web.search`/`web.fetch` and not spam the operator's real browser.
- Added durable `learning_observations` journal. `add_message`, `record_tool_run`
  and `delete_conversation` append learning observations, so deletion removes UI
  history but not the learning source trail.
- Learning tick now reads dialogue/web observations, supervisor runs learning once
  immediately on startup, and default learning interval is 120s.
- Command Center chat links are auto-linked for Markdown, `http(s)` and `www.`
  URLs; chat height now auto-stretches and can be resized beyond the old 760px cap.
- Tests added/updated around browser-open policy and learning journal retention.

### 2026-07-09 - Codex

- Added an operator queue/kernel surface: `GET /api/operator/queue` combines
  pending/executable approvals, blocked/running mission tasks, health warnings,
  generation truncation signals, memory hygiene, and future model-profile notes.
- Added lightweight model-profile roadmap via `GET /api/model-profiles`; current
  Gemma profiles stay active/available, 70B/80B planner and fast executor roles
  are scaffolded as future/inactive.
- Added memory hygiene reporting (`GET /api/memory/hygiene`) and consolidation
  endpoint (`POST /api/memory/consolidate`). Learning tick still performs
  consolidation automatically.
- Added auto-continuation for LLM answers stopped by `finish_reason=length`,
  including streamed answers. The assistant continues internally before exposing
  the old "token limit" warning.
- Command Center now has an operator queue tab, shows linked mission/task ids
  on approvals, and adds one-click approve+execute for pending gates.
- Regression tests: `test_agentic_answer_auto_continues_after_length_finish`
  and `backend/tests/test_operator_queue.py`.

### 2026-07-09 - Codex

- Added mission approval resume: when an agentic mission step asks for a gated
  tool, the approval payload now stores `mission_id`, `task_id`, and a compact
  tool-loop resume snapshot.
- `ApprovalExecutor` can execute the approved tool and call
  `AgentRuntime.resume_mission_after_approval`, feeding the tool observation
  back into the same agentic context. The mission task becomes `done` on success
  or stays `blocked` if the approved tool/resume fails or creates another gate.
- Approved mission tool runs are recorded with `mission_id/task_id`, completed
  resumed steps are saved to mission memory, and a `mission_step` event is emitted.
- Regression test: `test_approval_execution_resumes_blocked_mission_step`.
- Next useful step: show the linked mission/task directly inside each approval
  row and optionally add a one-click "approve and execute" button in Command Center.

### 2026-07-09 - Codex

- Integrated `origin/claude/admin-assistant-enhancements-ret1id` into `main`.
- Fixed mission approval propagation so a mission step that creates an approval
  is marked `blocked` instead of `done`.
- Fixed mission task updates to verify `mission_id` before mutating a task.
- Added regression coverage for both fixes.
- Current agreement: this file is the shared notebook for future assistant notes.
