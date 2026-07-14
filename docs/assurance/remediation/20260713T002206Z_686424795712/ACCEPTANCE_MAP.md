# Acceptance map — functional remediation

Run: `20260713T002206Z_686424795712`

Immutable source: `.audit/runs/20260713T002206Z_686424795712/functional`

Эта карта связывает каждый finding, Spark task, исходный user journey,
детерминированную проверку и post-fix acceptance. Она не меняет исходные
verdicts: новые результаты сохраняются только в commit chain remediation
worktree.

## Общий task gate

Task получает `PASS` только если одновременно:

1. исходный FAIL безопасно воспроизведён на isolated state;
2. focused contract regression test добавлен и PASS;
3. exact test command из task file и directly affected neighboring suite PASS;
4. bounded post-fix replay и требуемое число harmless journey repeats PASS;
5. claimed action совпадает с фактическим state/artifact;
6. stream/final output не содержит leaks, empty/duplicate/truncated finals или
   false success;
7. cleanup/rollback checks PASS;
8. diff ограничен task allowed files, `git diff --check` PASS, `.audit/**`
   неизменён по anchored external content manifests;
9. task report и patch находятся в одном локальном task commit.

Невоспроизведённый FAIL, отсутствующее evidence, deterministic FAIL,
`INCONCLUSIVE`, safety/restore failure или scope drift не являются PASS и
останавливают wave. Semantic reviewer не может отменить deterministic FAIL.

Для каждой task дополнительно выполняется связанный bounded subset:

```powershell
py -3.11 -m qa.cli validate-suite qa\suites\operator_core
py -3.11 -m qa.cli validate-evidence <new-sanitized-task-evidence.jsonl> --expected-manifest-sha256 <retained-sha256>
py -3.11 -m qa.cli replay <new-sanitized-task-evidence.jsonl> --expected-manifest-sha256 <retained-sha256> --output <replay.json>
```

Для semantic contracts создаются два раздельных review packet/output и
adjudication:

```powershell
py -3.11 -m qa.cli build-review-packets <new-sanitized-task-evidence.jsonl> --expected-manifest-sha256 <retained-sha256> --output-dir <packets>
py -3.11 -m qa.cli adjudicate <review-1.json> <review-2.json> --replay <replay.json> --evidence <new-sanitized-task-evidence.jsonl> --context-anchor-1 <sha256> --context-anchor-2 <sha256> --review-anchor-1 <sha256> --review-anchor-2 <sha256> --expected-manifest-sha256 <retained-sha256>
```

## Wave 0

### SPARK-0017 / FUNC-FIND-017 — doctor token redaction

- Before: `FUNC-0074` Compose-config doctor check exposed the runtime API
  token.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_smoke_script.py backend/tests/test_redaction.py -q`.
- Replay: run the doctor Compose-output fixture with a disposable canary and
  scan stdout, stderr, JSON and persisted task logs.
- PASS: exact canary count is zero in every output; redacted marker is present;
  exit code and machine result agree.

### SPARK-0016 / FUNC-FIND-016 — doctor exit-code truthfulness

- Before: `FUNC-0074` returned process exit 0 while required smoke result was
  false.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_smoke_script.py backend/tests/test_config_storage.py -q`.
- Replay: forced required failure with deployment home/profile/model variables
  removed, then clean controlled fixture.
- PASS: failure exits nonzero and names the check; clean suite exits zero;
  child tests do not inherit deployment variables.

### SPARK-0006 / FUNC-FIND-006 — internal tool-envelope leak

- Before: `OP-0025`, `OP-0028..OP-0030`, `OP-0034`, `OP-0036` and `OP-0044`
  rendered tool-shaped payloads.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_llm.py backend/tests/test_agent.py backend/tests/test_api_smoke.py -q`.
- Replay: scan every NDJSON delta, terminal answer and rendered DOM string for
  `call:`, tool/function envelopes, roles, transport frames, tracebacks and
  internal schemas.
- PASS: zero forbidden marker matches in all affected journeys and exactly one
  non-empty terminal answer.

### SPARK-0009 / FUNC-FIND-009 — canonical approval action

- Before: `OP-0037` repeats 1–3 bound `filesystem.mkdir` instead of canonical
  `fs.mkdir`.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_approval_executor.py backend/tests/test_tools.py backend/tests/test_agent.py -q`.
- Replay: use a task-owned temporary path; inspect pending action, approve and
  execute exactly once.
- PASS: three runs bind `fs.mkdir`, reject unknown aliases before pending
  approval, create only the approved path exactly once and record truthful
  audit state.

### SPARK-0015 / FUNC-FIND-015 — runtime-home transcript isolation

- Before: `FUNC-0085` preflight retained old GUI messages while the new backend
  had zero conversations.
- Deterministic tests: runtime-identity-keyed frontend test/typecheck command
  defined in `frontend/package.json` plus
  `py -3.11 -m pytest backend/tests/test_api_smoke.py -q`.
- Replay: three old-home to empty-new-home switches in the isolated runtime;
  compare DOM with `GET /api/conversations`.
- PASS: zero old messages after every switch and DOM/history exactly equals the
  new backend state.

### SPARK-0011 / FUNC-FIND-011 — interrupted-stream placeholder cleanup

- Before: `OP-0040` repeats 1–3 could retain an empty 0 ms assistant bubble.
- Deterministic tests: frontend placeholder rollback/terminal deduplication
  test/typecheck command plus
  `py -3.11 -m pytest backend/tests/test_api_smoke.py -q`.
- Replay: interrupt navigation before terminal state, return and retry three
  times.
- PASS: no empty, duplicate or stale final; exactly one retry terminal answer;
  persisted history matches DOM.

## Wave 1

### SPARK-0014 / FUNC-FIND-014 — repeated-start idempotency

- Before: `FUNC-0070` three warm repeats exited 1 with lease errors.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_runtime_lease.py backend/tests/test_deployment_contracts.py -q`.
- Replay: start one owned isolated stack, capture identities, invoke the same
  start command three times.
- PASS: all repeats exit 0, preserve process/container identities and
  truthfully report already running without a mutating post-lease check.

### SPARK-0007 / FUNC-FIND-007 — uploaded-document identity and recall

- Before: `OP-0016`, `OP-0026`, `OP-0032`, `OP-0039` and `OP-0050` lost or
  misresolved uploaded documents.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_document_memory.py backend/tests/test_document_runtime.py backend/tests/test_agent.py -q`.
- Replay: one controlled upload with retained source ID, then exact-name and
  exact-ID fresh/prior recall, comparison, retry and mission cases.
- PASS: every route resolves the same controlled source ID/fact and completes
  its read-only result without cross-conversation bleed.

### SPARK-0003 / FUNC-FIND-003 — atomic artifact path/write/verification

- Before: `OP-0013`, `OP-0029..OP-0031` and `OP-0034` produced wrong paths,
  incomplete transforms, pseudo-tool output or invalid native structure.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_document_runtime.py backend/tests/test_tools.py -q`.
- Replay: exact destination, copy-only, Markdown-to-DOCX and three concurrent
  outputs under a task-owned temporary directory; compare source/output hashes
  and DOCX ZIP/XML structure.
- PASS: exact paths/types and distinct collision-free outputs exist, native
  structure validates and every source hash is unchanged.

### SPARK-0008 / FUNC-FIND-008 — corrupt-document recovery

- Before: `OP-0033` repeat 3 lacked a clean actionable corrupt-to-valid retry.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_document_runtime.py backend/tests/test_file_types_and_archives.py -q`.
- Replay: three separate corrupt PDFs followed by their valid replacements.
- PASS: each run returns one normalized actionable error, persists no partial
  result and then returns one clean valid result without stale content.

## Wave 2

### SPARK-0002 / FUNC-FIND-002 — exact response constraints

- Before: `OP-0007`, `OP-0010` and `OP-0024` breached count, JSON/schema or
  assumption contracts.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_verification.py backend/tests/test_agent.py -q`.
- Replay: exact catalog requests with bullet-count, JSON parse/schema and
  assumption validators.
- PASS: every validator passes in three consecutive runs.

### SPARK-0004 / FUNC-FIND-004 — multi-turn references

- Before: `OP-0014`, `OP-0016` and `OP-0032` lost selected options/files.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_agent.py backend/tests/test_document_memory.py -q`.
- Replay: one clean conversation per case, first turn completed, then exact
  pronoun/prior-file follow-up without restatement.
- PASS: exact conversation-local object resolves in three repeats and no
  cross-window state is used.

### SPARK-0005 / FUNC-FIND-005 — clarification before mission

- Before: `OP-0023` repeats 1–2 created a mission before resolving ambiguity.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_agent.py backend/tests/test_executive_planner.py -q`.
- Replay: inspect conversations, missions and files before answering the
  single clarification, then answer it.
- PASS: exactly one concise question, zero mission/artifact writes before the
  answer and correct resumption afterward.

### SPARK-0012 / FUNC-FIND-012 — memory namespace isolation

- Before: `OP-0041` repeats 1–2 wrote under `operator` instead of
  `audit.functional.20260713`.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_cognitive_memory.py backend/tests/test_persona.py backend/tests/test_api_smoke.py -q`.
- Replay: query requested, operator and default namespaces before/after
  controlled writes and recall.
- PASS: exact requested namespace contains the marker; operator/default and
  persona state remain unchanged.

### SPARK-0001 / FUNC-FIND-001 — DNS/network versus shopping routing

- Before: `OP-0006` repeats 1–2 routed a direct DNS request to shopping.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_shop_routing.py backend/tests/test_tools.py -q`.
- Replay: submit the exact one-sentence DNS request twice in separate chats.
- PASS: both traces select DNS/network lookup, no shop tool appears and each
  final is one factual sentence or the exact actionable network error.

### SPARK-0010 / FUNC-FIND-010 — cited usable web synthesis

- Before: `OP-0038` repeats 1–3 failed the evidence/usability rubric.
- Deterministic tests:
  `py -3.11 -m pytest backend/tests/test_web_surfer_integration.py backend/tests/test_web_orchestrator.py backend/tests/test_agent.py -q`.
- Replay: controlled public query; validate each direct URL and its adjacent
  claim, or the adapter-unavailable fixture.
- PASS: three runs contain supported claims with direct URLs, or each returns
  one precise actionable unavailability message without false success.

## Product decision gate

`SPARK-0013 / FUNC-FIND-013` не имеет wave acceptance до human decision.
`PROFILE-SAFETY` и `PROFILE-RESEARCH` критерии определены в
`PROFILE_DECISION.md`. Ни один synthetic, deterministic или semantic verdict
не может автоматически сертифицировать 31B profile без обязательных live
health и bounded GUI gates.

## Wave-level candidate gate

Wave становится `WAVE_N_CANDIDATE_FOR_REVIEW` только если все её tasks PASS в
точном порядке, task-to-commit mapping полный, batch deterministic suites и
bounded replay PASS, два semantic outputs сохранены раздельно где нужны,
adjudication не скрывает disagreement, cleanup/rollback PASS, worktree clean,
exact `REVIEWED_INPUT_COMMIT` совпадает со start HEAD, а anchored external
before/after manifests подтверждают неизменность всего `.audit/**` tree,
включая untracked files; push/merge не выполнялись.

Даже `WAVE_2_CANDIDATE_FOR_REVIEW` не является product READY. Только отдельная
post-fix acceptance campaign может решить вопрос о создании
`functional/READY`.
