# JARVIS — PHASE B: ПОЛНАЯ ПРОВЕРКА КАЧЕСТВА НА ЖИВОЙ ЦЕЛЕВОЙ МАШИНЕ

Ты работаешь как главный инженер по качеству, SRE, системный архитектор, специалист по надёжности и координатор независимых рабочих потоков. Это **вторая половина двухкомпонентного доказательного аудита JARVIS**.

PHASE A уже должна была разобрать репозиторий без доступа к целевой машине, создать карту системы, поведенческие контракты, findings и точную очередь живых сценариев. Твоя задача — продолжить **тот же audit run** на реальной Windows-машине, проверить всё, что нельзя доказать чтением кода, подтвердить или опровергнуть выводы PHASE A, дополнить пропущенные функциональные сценарии и подготовить окончательную атомарную очередь исправлений для Codex Spark.

Это авторизованная проверка принадлежащего пользователю локального проекта. Цель — корректность, устойчивость, целостность данных, предсказуемость, качество UX и восстановимость. Не выполняй активные наступательные проверки, не формируй инструкции по нарушению ограничений и не воздействуй на внешние системы. Проверки ввода, разрешений, URL, файловых путей и чувствительных данных выполняй только на безвредных синтетических примерах, loopback fixtures, временных каталогах и копиях состояния.

Подробные результаты сохраняй в `.audit/**` и во внешних evidence-каталогах. В чат выводи только короткие отчёты на русском языке: процент готовности, counts, paths, blockers и безопасные следующие действия. Не публикуй большие raw-логи, длинные тестовые входы или содержимое конфиденциальных файлов.

---

## 0. Фиксированные каталоги

```text
D:\jarvis-gpt   — единственный рабочий Git-репозиторий и исходный код
D:\jarvis       — модели, Docker/runtime-данные, кеши, пользовательские данные,
                  большие логи и тяжёлые evidence-файлы
```

Начни:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
git status --short --branch
```

Ожидаемый Git root — `D:/jarvis-gpt` с допустимой разницей в регистре и разделителях. `D:\jarvis` никогда не считай репозиторием и не создавай там `.audit`.

Малые текстовые audit-артефакты:

```text
D:\jarvis-gpt\.audit\...
```

Тяжёлые screenshots, видео, dumps, traces, profiling и длинные логи:

```text
D:\jarvis\audit-evidence\<RUN_ID>\...
```

Для каждого внешнего evidence-файла сохраняй путь, SHA-256, размер, тип, timestamp, sanitization и связанные scenario/finding IDs.

---

## 1. Обязательный handoff из PHASE A

До runtime-действий проверь:

```text
.audit/LATEST_STATIC_RUN.txt
```

Он должен содержать:

```text
.audit/runs/<RUN_ID>
```

В run-каталоге обязательны как минимум:

```text
PIPELINE_STATE.json
SOURCE_BASELINE.json
PHASE_A_COMPLETION.md
STATIC_EXECUTIVE_SUMMARY.md
SYSTEM_MAP.md
FEATURE_CATALOG.md
BEHAVIORAL_CONTRACT.md
STATIC_SCENARIO_MATRIX.csv
STATIC_FINDINGS_INDEX.md
LIVE_AUDIT_PLAN.md
LIVE_SCENARIO_QUEUE.csv
SOURCE_DRIFT_POLICY.md
handoff/PHASE_B_START_HERE.md
```

Если handoff отсутствует, `phase_a.status` равен `IN_PROGRESS`/`INCOMPLETE` или live-очередь не читается, не изображай цельное продолжение. Зафиксируй blocker и остановись.

`COMPLETE_WITH_BLOCKERS` допустим: прочитай блокеры и продолжай всё, что доступно на живой машине.

Сначала прочитай:

1. `handoff/PHASE_B_START_HERE.md`;
2. `PIPELINE_STATE.json`;
3. `SOURCE_BASELINE.json`;
4. `SOURCE_DRIFT_POLICY.md`;
5. `BEHAVIORAL_CONTRACT.md`;
6. `STATIC_FINDINGS_INDEX.md` и только необходимые findings;
7. `LIVE_SCENARIO_QUEUE.csv`;
8. `SPEC_GAPS.md`, `TEST_GAPS.md`, `ARCHITECTURE_RISKS.md`;
9. актуальные repository instructions.

Не полагайся на контекст старой сессии. Единственный источник PHASE A — сохранённые файлы.

### Возобновление после оборванной попытки PHASE B

Если run уже содержит `LIVE_*`, `AUDIT_STATE.md` или частично обновлённую очередь:

1. не удаляй их;
2. проверь hashes, timestamps, source commit и фактический runtime state;
3. отдели доказанный результат от незавершённой записи;
4. повтори только проверки с неполным evidence или изменившимися preconditions;
5. запиши точную точку продолжения в `AUDIT_STATE.md`;
6. не объявляй старый chat-ответ доказательством.

---

## 2. Проверка source drift

Сравни текущий production tree с `source_commit` из `SOURCE_BASELINE.json`, исключая:

```text
.audit/**
docs/audit/**
```

Если production-код не изменился, продолжай тот же `RUN_ID`.

Если изменился:

1. создай `SOURCE_DRIFT.md`;
2. перечисли commits и changed files;
3. сопоставь их с FEAT/REQ/INV/SCN IDs;
4. перечитай затронутые production-файлы;
5. повтори релевантные static/hermetic checks;
6. переоцени findings и live scenarios;
7. не переноси PASS или root cause автоматически.

При небольшом управляемом drift продолжай тот же run. При крупном drift создай derived run с явной ссылкой `derived_from`.

Никогда не сбрасывай пользовательские изменения и не присваивай себе dirty working tree.

---

## 3. Scope

Первичная установка исключена. Считай, что изделие уже подготовлено, модели и локальные assets существуют, а базовый запуск в принципе возможен.

В scope входят свойства готового изделия:

- cold/warm start, stop, restart и recovery;
- частично запущенный и деградированный стек;
- фактические profiles, model mapping и config precedence;
- offline-first после подготовки;
- backend, REST, streaming, WebSocket и GUI;
- локальная LLM, provider и tool loop;
- missions, memory, persona, learning, autonomy и approvals;
- browser/web/document/filesystem/Docker/host tools в разрешённых рамках;
- persistence, copied-state recovery и data integrity;
- concurrency, cancel, retry и idempotency;
- input/access/data boundaries на безвредных локальных fixtures;
- performance, resource use, repeated cycles и bounded soak.

Фактический список профилей и функций бери из текущего репозитория и PHASE A. Не навязывай устаревшее количество профилей.

---

## 4. Правила работы на живой машине

1. Не уничтожай пользовательские данные, модели, Docker volumes, WSL distributions, секреты или незакоммиченные изменения.
2. Запрещены `git reset --hard`, агрессивный `git clean`, broad Docker prune, удаление рабочих volumes, factory reset и массовое удаление runtime-каталогов.
3. Не исправляй production-код во время PHASE B. Разрешены `.audit/**`, внешние evidence и изолированные harness/repro/fixtures.
4. Проверки readonly/locked/nearly-full/damaged-state выполняй только на копиях DB, synthetic data, temp roots, отдельном Compose project или test volume.
5. До остановки процесса, Docker/WSL component или порта докажи ownership. Не затрагивай посторонние workloads.
6. Не обращайся к внешним целям для проверки защитных границ. Используй loopback servers и synthetic resources.
7. Не выполняй потенциально опасные model-generated команды на реальном host. Используй mock, dry-run, allowlist и безвредные цели.
8. Используй synthetic sentinels вместо реальных токенов и PII. Маскируй значения в evidence.
9. Не устанавливай и не обновляй runtime-зависимости продукта без отдельной необходимости. Временные audit tools изолируй и фиксируй версии.
10. Не push и не открывай PR без прямого указания пользователя.
11. Если сценарий нельзя выполнить безопасно и локально, установи `BLOCKED_BY_SAFETY` или `BLOCKED_BY_POLICY` и продолжи независимые направления.
12. Runtime PASS требует реально выполненного сценария и проверенного oracle.
13. После каждого изменяющего runtime сценария верни документированное состояние и выполни normal smoke.
14. Следуй `04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md` во всём, что касается backups, worktree, snapshots и восстановления.

---

## 5. Статусы и evidence

Для live-сценариев:

- `PASS` — реально выполнен, oracle проверен, evidence сохранён;
- `FAIL` — наблюдаемое поведение нарушает контракт;
- `BLOCKED_BY_ENV` — отсутствует конкретный ресурс;
- `BLOCKED_BY_SAFETY` — нет проверенной изоляции/восстановления;
- `BLOCKED_BY_POLICY` — проверка требует недопустимого воздействия или деталей;
- `BLOCKED_BY_SPEC` — ожидаемое поведение неоднозначно;
- `NOT_APPLICABLE` — доказуемо неприменимо;
- `INCONCLUSIVE` — данные противоречивы или flaky;
- `NOT_RUN` — допустим только до завершения PHASE B.

Каждый подтверждённый defect воспроизведи минимум дважды, если это безопасно и разумно.

Для каждой команды/GUI-последовательности сохраняй:

- UTC timestamp;
- exact command/action;
- working directory;
- profile и релевантные env values с redaction;
- state before/after;
- exit code;
- duration;
- stdout/stderr/log/screenshot paths;
- process/container/resource deltas;
- SCN/TEST/EVID/finding IDs;
- cleanup result.

Разделяй defect, limitation среды, SPEC-GAP, TEST-GAP, performance observation и recommendation.

---

## 6. Продолжение единого run-каталога

Продолжай каталог из `.audit/LATEST_STATIC_RUN.txt`.

Добавь или обнови:

```text
.audit/runs/<RUN_ID>/
  PIPELINE_STATE.json
  AUDIT_STATE.md
  LIVE_BASELINE.md
  LIVE_ENVIRONMENT.json
  LIVE_TEST_RESULTS.md
  LIVE_SCENARIO_MATRIX.csv
  LIVE_FINDINGS_INDEX.md
  PROFILE_AND_MODEL_PROOF.md
  OFFLINE_PROOF.md
  GUI_TEST_REPORT.md
  CONCURRENCY_AND_RECOVERY_REPORT.md
  DATA_BOUNDARY_REPORT.md
  PERFORMANCE_REPORT.md
  SOAK_REPORT.md
  COMBINED_EXECUTIVE_SUMMARY.md
  ASSURANCE_STATEMENT.md
  COVERAGE_REPORT.md
  TRACEABILITY.csv
  FINDINGS_INDEX.md
  SPEC_GAPS.md
  TEST_GAPS.md
  BLOCKERS.md
  RESIDUAL_RISK.md
  AUDIT_JOURNAL.md
  EVIDENCE_MANIFEST.json
  findings/
  evidence/live/
  harness/live/
  repros/
  spark/
    READY
    START_HERE_FOR_SPARK.md
    SPARK_MASTER_PROMPT.md
    SPARK_QUEUE.csv
    SPARK_PROGRESS.md
    TASK_SCHEMA.md
    tasks/
  machine/
    scenario_results.jsonl
    findings.jsonl
    spark_tasks.jsonl
```

PHASE A raw evidence не перезаписывай. Исправления factual errors оформляй addendum/journal entry.

Только после всех критериев завершения создай:

```text
.audit/LATEST_COMPLETE_RUN.txt
.audit/LATEST_RUN.txt
.audit/runs/<RUN_ID>/spark/READY
.audit/runs/<RUN_ID>/spark/safety/READY
```

Если PHASE B оборвалась, имеет необработанные `NOT_RUN` или не прошёл safety gate, markers запрещены.

---

## 7. Организация рабочих потоков

Если доступны субагенты, раздели read-only исследование:

1. runtime/launcher/profiles/Docker/WSL/offline;
2. backend/API/stream/WebSocket;
3. LLM/agent/tools/missions/memory;
4. frontend/GUI/accessibility/service worker;
5. persistence/concurrency/recovery;
6. input/access/data boundaries;
7. performance/resources/soak;
8. независимый reviewer полноты.

Действия, меняющие один runtime, Docker project, GPU, model server, persistent store или GUI state, выполняй строго последовательно.

Главный агент:

- назначает ownership сценариям;
- проверяет первичный evidence;
- дедуплицирует findings по root cause;
- обновляет `AUDIT_STATE.md` после каждого крупного блока;
- после compaction перечитывает pipeline state и очередь;
- сохраняет точную следующую команду.

Сообщи пользователю прогресс на 50% и 90%. Раньше сообщай только о реальном blocker или необходимости решения пользователя. Все сообщения — на русском.

---

## 8. PHASE B0 — preflight и baseline

1. Проверь Git root, branch, status, source drift и handoff.
2. Зафиксируй Windows build, PowerShell, Python, Node/npm, Docker Desktop/Engine/Compose, WSL, GPU, driver, CUDA, RAM, VRAM, CPU, disks/free space.
3. Зафиксируй фактические runtime/data/model/cache/log/state roots без раскрытия секретов.
4. Сними processes, services, listening ports, containers, networks, volumes и baseline resources.
5. Определи штатные start/stop/restart/status/doctor/dispatcher commands.
6. Проверь наличие локальных моделей/images без скачивания.
7. Зафиксируй initial state до изменений.
8. Выполни безопасный baseline smoke.
9. Сопоставь health/readiness signals.
10. Выполни безопасные repository tests/static checks на целевой ОС.
11. Создай предварительный backup plan до сценариев, меняющих runtime state.

`LIVE_BASELINE.md` должен позволять отличить defect от environment/configuration issue.

---

## 9. PHASE B1 — исполнение live-очереди

Исполняй `LIVE_SCENARIO_QUEUE.csv` по dependencies и priority. Для каждой строки:

1. проверь preconditions;
2. создай требуемую изоляцию;
3. запиши state before;
4. выполни stimulus;
5. проверь каждый oracle/invariant;
6. собери telemetry/evidence;
7. выполни cleanup;
8. проверь возврат к known-good state;
9. обнови status и findings.

Если живая система открыла новый компонент или failure mode, добавь новый SCN с traceability.

Для комбинаций используй pairwise; для high-risk пересечений — ограниченное 3-way; для конечных state machines — targeted exhaustive; для sequences — bounded seeded exploration; для inputs — equivalence classes и boundaries.

Никаких неограниченных random runs.

---

## 10. PHASE B2 — profiles, model mapping, Docker/WSL и offline

Для каждого фактически активного профиля докажи:

```text
launcher/CLI selection
  -> env/config precedence
  -> resolved service command
  -> container/process runtime
  -> provider metadata/health
  -> реально загруженная model identity/config
  -> UI/API-reported profile/model
```

Не принимай env variable за доказательство загрузки модели.

Проверь:

- cold/warm start;
- start из другого cwd;
- paths с пробелами/кириллицей;
- repeat start;
- stop/restart всей системы и отдельных components;
- partial stack;
- stale PID/lock/container/network;
- occupied ports;
- unknown/misspelled profile;
- empty/malformed/conflicting env;
- config precedence;
- model/profile mismatch и stale dispatcher;
- startup/readiness races;
- health false-positive/false-negative;
- component interruption и recovery;
- process/container/network/port/GPU cleanup;
- repeated cycles и накопление мусора;
- понятность диагностики.

### Offline-proof

После подтверждения локальных assets:

1. зафиксируй baseline network activity;
2. используй scoped/reversible offline mode, сохраняя loopback/local dependencies;
3. запусти штатно без build/pull/download/package resolution;
4. наблюдай неожиданные DNS/HTTP/registry/package attempts;
5. докажи работу локального ядра;
6. проверь понятную деградацию явно сетевых функций;
7. восстанови сеть и проверь recovery.

Создай `OFFLINE_PROOF.md` с методом, captures, результатом по каждому профилю и ограничениями доказательства.

---

## 11. PHASE B3 — backend, API, streaming, WebSocket и GUI

### API/stream/WebSocket

Для каждого endpoint/event из `FEATURE_CATALOG` проверь:

- normal request/response;
- missing/extra/wrong/oversized/malformed payload;
- стабильную error semantics и отсутствие false success;
- bind/origin/session assumptions;
- timeout/cancel/retry/idempotency;
- slow consumer/backpressure;
- reconnect, duplicate connection, half-open и abrupt close;
- restart во время stream;
- missing/duplicate/late/out-of-order terminal events;
- cross-session isolation;
- correlation/logging/redaction;
- backend ↔ frontend schema compatibility.

### GUI

Проверяй взаимодействием, а не только screenshot:

- empty/ready state;
- chat submit/stream/cancel/retry;
- tool progress и approvals;
- backend/model/tool/network errors;
- offline/reconnect;
- history restore;
- missions, memory, files, diagnostics, status и панели;
- profile/model/status truth;
- отсутствие stale optimistic success;
- duplicate submit/event prevention;
- resize/maximize/restore/scroll/focus;
- 1280×720, 1920×1080 и доступные DPI;
- длинные сообщения, code, tables, URLs, кириллица, emoji;
- keyboard navigation и clipboard;
- focus visibility, labels, semantic roles;
- service-worker cache/update/offline shell;
- safe text rendering;
- отсутствие лишних внутренних/legacy labels;
- понятные recovery actions.

Каждый visual finding: steps, state, viewport, DPI, screenshot, expected/observed, reproducibility.

---

## 12. PHASE B4 — LLM, agent, tools, missions и memory

Разделяй:

1. deterministic orchestration с fake/mock provider;
2. real-model behavioral smoke/robustness.

Не используй exact nondeterministic text как единственный oracle. Проверяй schemas, side effects, approvals, terminal states, citations/provenance и bounded semantic rubrics.

Проверь:

- empty/short/long/context-near-limit requests;
- Unicode, кириллицу, emoji, Markdown, code, JSON/XML и длинные строки;
- timeout, slow tokens, disconnect, empty response, malformed/duplicate chunks;
- invalid tool names/args/types/JSON;
- multiple/dependent tool calls и partial success;
- timeout/cancel/retry/idempotency around tools;
- cancel до/во время/после разрешённого side effect;
- huge or unusual outputs в ограниченном synthetic fixture;
- planner/executor disagreement;
- loop/step budgets;
- provider unavailable/degraded/recovered;
- честный failure вместо false success;
- untrusted text remains data and does not silently change permissions/workflow;
- synthetic sentinel remains redacted;
- filesystem/root/path boundary на temp roots;
- approval exact binding and one-shot semantics;
- cross-session isolation;
- mission create/run/resume/block/complete/final report;
- restart с незавершённой mission;
- memory/persona/learning write/read/dedup/delete/isolation;
- autonomy/self-heal без скрытых state changes;
- displayed steps match actual actions.

Для каждого зарегистрированного tool используй безвредную матрицу:

```text
success, invalid input, denied, timeout, cancel, partial, bounded large result,
retry, concurrent use, unsupported request, encoding/path edge case
```

### Web/browser/document/file surfaces

Используй только локальные fixtures и synthetic documents:

- URL destination policy и redirects;
- unsupported schemes и malformed URLs;
- untrusted page/document content remains data;
- provenance/citation correctness;
- cache freshness/stale labeling;
- browser approval/session/tab isolation;
- timeout/cancel/recovery;
- filename/path boundary на temp roots;
- MIME/extension mismatch;
- bounded oversized/compressed inputs;
- Word/Excel/PDF/PPTX/text extraction errors;
- formulas/external links/macros treated as data;
- copy-on-write, original preservation и output collisions;
- Unicode/encoding/OCR fallback;
- chunk/index/recall consistency;
- parser failure isolation.

Не обращайся к внешним системам ради этих проверок.

---

## 13. PHASE B5 — concurrency, recovery и data integrity

На копиях и test roots проверь:

- несколько запросов в одной и разных сессиях;
- parallel tool calls;
- cancel racing terminal event;
- новый request сразу после cancel;
- backend restart during stream;
- frontend/WebSocket/provider/tool-worker interruption;
- delayed/duplicate/out-of-order events;
- lock contention;
- process stop during write на изолированной копии;
- read-only state directory;
- nearly-full test volume;
- temporary store unavailability;
- partial/damaged config/cache/index/session/DB copy;
- clock/timeout boundaries;
- rapid repeated start/stop/restart;
- orphan tasks/processes/containers/ports/GPU allocations;
- recovery to known-good state.

Для SQLite/WAL проверь transaction atomicity, schema/migrations, idempotency, duplicate events, lock retries, integrity check, backup/restore и diagnostics.

После каждого interruption:

1. проверь видимый результат;
2. проверь persisted copy;
3. проверь logs/audit events;
4. восстанови систему;
5. повтори normal smoke;
6. сравни baseline counts/hashes/invariants.

---

## 14. PHASE B6 — input, access и data boundaries

Это review защитного поведения, а не активное наступательное тестирование. Используй только harmless no-op examples, synthetic sentinels, local fixtures и temporary roots.

Проверь:

- action/command validation до исполнения;
- allowed filesystem roots и canonical paths;
- exact approval binding;
- schema enforcement;
- local API bind/origin/session defaults;
- URL destination restrictions на loopback fixtures;
- sensitive-value redaction в logs/UI/errors/audit/model context;
- retry behavior for state-changing actions;
- permission denial produces clear failure;
- untrusted text cannot silently obtain action privileges;
- unsupported file/URL/input forms fail closed;
- dependency/image pinning and reproducibility.

Не генерируй operational abuse instructions и не выполняй проверки против внешних целей. Если для доказательства требуется более широкий метод, установи `BLOCKED_BY_POLICY` и сохрани review-only finding.

Создай `DATA_BOUNDARY_REPORT.md` с contracts, benign checks, PASS/FAIL/BLOCKED и residual risk.

---

## 15. PHASE B7 — performance, resources и soak

Для каждого профиля измерь:

- cold/warm startup и time-to-ready;
- time-to-first-token и completion latency;
- API/UI latency без LLM и с LLM;
- throughput/queue/backpressure при ограниченной параллельности;
- CPU/RAM/VRAM/disk I/O/network;
- resource release after completion/cancel/shutdown;
- repeated start/stop and request cycles;
- log/cache/history/index/temp growth;
- long conversation/mission;
- bounded soak;
- recovery after resource pressure.

Отделяй warmup от steady state, выполняй несколько измерений и фиксируй условия. Не называй observation defect без contract/SLO или явной непригодности.

Soak имеет watchdog, resource/disk ceilings и safe abort. После него — smoke и leak/orphan check.

---

## 16. Подтверждение PHASE A

Для каждого finding PHASE A:

- `static-confirmed`: проверь runtime там, где это повышает уверенность;
- `probable-runtime`: выполни связанный scenario и переведи в confirmed/refuted/inconclusive;
- `spec-gap`: попытайся разрешить по public behavior/docs, не выдумывая решение;
- `test-gap`: создай точную test-foundation task;
- `inconclusive`: собери evidence либо оставь blocker.

Не удаляй refuted finding; сохрани историю и evidence.

Новые findings нумеруй продолжая последовательность. Дедуплицируй root cause.

---

## 17. Формат финального finding

```yaml
id: JARVIS-0001
title: "..."
kind: defect | reliability | data-integrity | performance | ux |
  access-boundary | spec-gap | test-gap
severity: critical | high | medium | low
priority: P0 | P1 | P2 | P3
status: confirmed | refuted | blocked-by-spec | blocked-by-env |
  blocked-by-safety | blocked-by-policy | inconclusive | accepted-risk
confidence: 0.00-1.00
reproducibility: always | intermittent | static-proof | not-reproduced
components: []
profiles: []
feature_ids: []
requirement_ids: []
scenario_ids: []
evidence_ids: []
affected_paths: []
spark_task_ids: []
```

Разделы:

1. Summary.
2. Contract/invariant and impact.
3. Preconditions.
4. Exact benign reproduction.
5. Expected vs observed.
6. Evidence paths/excerpts.
7. Reproduction count/flakiness.
8. Root cause: confirmed/hypothesis/unknown.
9. Code/data/control flow.
10. Data-integrity/privacy/permission implications.
11. Safe workaround, если есть.
12. Remediation direction.
13. Regression risks.
14. Binary acceptance criteria.
15. Related findings/gaps.
16. Spark task mapping or `NO_CODE_CHANGE`.

Не завышай severity и не включай operational abuse details в user-facing summaries.

---

## 18. Итоговая очередь Spark

Каждый confirmed code/config/test defect преобразуй в атомарную task. Spark не должен повторять исследование.

Правила:

- одна task = одна root cause/подготовительный шаг;
- предпочтительно одна подсистема и до пяти production-файлов;
- крупную проблему раздели на test foundation, small implementation steps и validation checkpoint;
- неоднозначный product decision получает `BLOCKED_BY_SPEC`;
- укажи dependencies, conflicts, order и batches;
- после каждого batch создай validation task;
- tasks, меняющие общий runtime/state/GPU или те же files, не выполняются параллельно;
- regression tests используют harmless synthetic cases;
- task, требующая недопустимого reproduction, получает `BLOCKED_BY_POLICY` и review-only guidance.

Формат task front matter:

```yaml
id: SPARK-0001
title: "..."
source_findings: []
type: investigation | test | implementation | refactor | documentation | validation
status: READY | BLOCKED_BY_SPEC | BLOCKED_BY_ENV | BLOCKED_BY_SAFETY |
  BLOCKED_BY_POLICY | DONE
priority: P0 | P1 | P2 | P3
risk: low | medium | high
confidence_in_root_cause: 0.00-1.00
estimated_scope: tiny | small | medium
profiles: []
components: []
context_files: []
allowed_files: []
forbidden_files: []
depends_on: []
conflicts_with: []
recommended_batch: 1
requires_real_model: true | false
requires_docker_restart: true | false
requires_gui_check: true | false
mutation_class: code_only | runtime_read_only | runtime_ephemeral |
  persistent_state_mutating | docker_topology_mutating | high_risk_blocked
mutable_roots: []
requires_pre_task_snapshot: true | false
human_gate_required: true | false
```

Каждая task содержит:

1. Goal и contract.
2. Known-good baseline/run/commit/profile.
3. Exact harmless FAIL reproduction.
4. Evidence paths.
5. Confirmed root cause или marked hypothesis.
6. Relevant code map.
7. Allowed/forbidden scope.
8. Minimal fix strategy.
9. Test-first requirement.
10. Exact validation commands.
11. Binary acceptance criteria.
12. Regression checklist.
13. Cleanup/restore procedure.
14. Stop/escalation conditions.
15. Suggested local commit message.

Создай `SPARK_QUEUE.csv`, `SPARK_PROGRESS.md`, `SPARK_MASTER_PROMPT.md`, `START_HERE_FOR_SPARK.md` и `TASK_SCHEMA.md`.

`SPARK_MASTER_PROMPT.md` должен соответствовать актуальным:

```text
docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
docs/audit/03_JARVIS_SPARK_REMEDIATION_PROMPT.md
docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md
```

---

## 19. Подготовка изолированного remediation-контура

До Spark выполни protocol `04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md`:

- классифицируй mutation scope tasks;
- создай safety artifacts;
- верни runtime в known-good state;
- создай отдельный worktree и branch;
- создай tags и verified Git bundle;
- создай verified runtime checkpoint для затрагиваемого состояния;
- выполни пробное восстановление;
- запусти consistency gate;
- только затем создай `spark/safety/READY` и `spark/READY`.

Если checkpoint/restore не доказаны, state-mutating tasks блокируются, но code-only tasks могут остаться READY при исправных Git safeguards.

---

## 20. Traceability и consistency

Финальная цепочка:

```text
feature -> requirement/invariant -> static/live scenario -> result
        -> evidence -> finding -> Spark task -> validation checkpoint
```

Consistency harness обнаруживает:

- unknown/duplicate IDs;
- feature без contract/spec-gap;
- high-risk requirement без positive/error/recovery scenario;
- live `NOT_RUN` в завершённом аудите;
- FAIL без finding;
- confirmed finding без evidence;
- code defect без Spark task или `NO_CODE_CHANGE`;
- task без finding/acceptance/tests;
- broken paths;
- duplicate queue order;
- cyclic dependencies;
- READY task с невыполненной dependency;
- обычный READY без safety READY.

Harness должен PASS до markers.

---

## 21. Критерии завершения PHASE B

Не ставь `phase_b.status = COMPLETE` и не создавай READY markers, пока:

1. PHASE A handoff проверен и source drift обработан.
2. Снята живая environment/baseline картина.
3. Все активные profiles запущены либо имеют конечный blocker.
4. Profile → model mapping доказан.
5. Offline-first проверен наблюдением runtime.
6. Безопасные штатные tests на Windows выполнены.
7. Каждая public feature имеет happy/error/recovery coverage или documented gap.
8. API/stream/WebSocket/GUI функционально проверены.
9. LLM/agent/tools/memory/missions имеют deterministic и real-model coverage.
10. High-risk concurrency/cancel/retry/recovery/data-integrity transitions выполнены либо заблокированы.
11. Input/access/data boundaries проверены только benign methods.
12. Performance baseline, repeated cycles и bounded soak зафиксированы.
13. Findings PHASE A получили конечный status.
14. Live matrix не содержит `NOT_RUN`.
15. Findings имеют evidence/reproduction/root cause/confidence.
16. Confirmed code defects преобразованы в atomic tasks либо имеют rationale.
17. Queue имеет dependencies/conflicts/batches/validation checkpoints.
18. Safety/rollback protocol выполнен.
19. Traceability consistency PASS.
20. `RESIDUAL_RISK.md` перечисляет недоказанное.
21. Production-код не исправлялся во время аудита, данные не повреждены, cleanup проверен.

`COMPLETE_WITH_BLOCKERS` допустим, если вся доступная работа выполнена, blockers конечны, а READY tasks не зависят от недоказанных контрактов. `spark/READY` разрешён только для строго отделённых tasks и только вместе с `spark/safety/READY`.

---

## 22. Итоговые документы

`COMBINED_EXECUTIVE_SUMMARY.md`:

- RUN_ID, source/current commit, branch и environment;
- scope PHASE A + B;
- counts features/requirements/scenarios/tests;
- PASS/FAIL/BLOCKED/INCONCLUSIVE;
- findings по severity/status;
- top systemic root causes;
- verdict по profiles/models, offline, GUI, agent/tools, persistence, boundaries, performance;
- READY/BLOCKED Spark tasks;
- residual risks.

`ASSURANCE_STATEMENT.md` отвечает:

> Что о качестве JARVIS подтверждено, в каких условиях, какими evidence и чего аудит всё ещё не гарантирует?

Не заявляй, что ошибок больше нет или проверены абсолютно все состояния.

`RESIDUAL_RISK.md` включает blockers, platform variants, nondeterministic areas, external dependencies, soak limits, spec gaps, assumptions и непроверенные combinations.

Обнови `PIPELINE_STATE.json`.

---

## 23. Финальный ответ

Все сообщения только на русском.

Не вставляй raw-логи и подробные тестовые входы. Сообщи:

- путь из `.audit/LATEST_COMPLETE_RUN.txt` или blocker;
- source/current commit и drift;
- проверенные profiles и model mapping;
- counts scenarios/results/findings;
- READY/BLOCKED Spark tasks;
- пять наиболее важных проблем только на высоком уровне;
- residual risks;
- путь к `spark/START_HERE_FOR_SPARK.md`;
- создан ли `spark/safety/READY`;
- создан ли обычный `spark/READY`;
- worktree/branch/bundle/checkpoint status;
- конечное состояние JARVIS, Docker и LLM.

Большое резюме без реально выполненных live-сценариев, evidence, конечной матрицы, traceability, safety gate и атомарной очереди считается незавершённой PHASE B.
