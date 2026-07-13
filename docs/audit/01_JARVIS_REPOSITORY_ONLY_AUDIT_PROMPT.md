# JARVIS — PHASE A: ПОЛНЫЙ РЕПОЗИТОРНЫЙ АУДИТ БЕЗ ЖИВОЙ МАШИНЫ

Ты работаешь как главный инженер по качеству, надёжности и архитектуре ПО. Выполни **первую половину двухкомпонентного доказательного аудита JARVIS**: максимально глубокое исследование текущего репозитория, которое возможно без доступа к целевой Windows-машине, Docker/WSL/GPU, локальным моделям, пользовательским данным и реально запущенному приложению.

Это защитный аудит принадлежащего пользователю проекта. Его цель — качество, корректность, устойчивость, сопровождаемость и безопасный дизайн. Не выполняй активные атаки, не создавай эксплуатационные сценарии, вредоносные payloads или инструкции по компрометации систем. Связанные с защитой выводы формулируй как review архитектуры, валидации, разрешений, обработки чувствительных данных и границ доверия. Любую проверку, которой нужен реальный runtime, переноси в PHASE B как безопасный функциональный сценарий без наступательных деталей.

После PHASE A локальный Sol Ultra продолжит тот же audit run на живой машине. Только после PHASE B будет создана окончательная атомарная очередь исправлений для Codex Spark.

---

## 0. Контракт кампании

```text
PHASE A — этот prompt, Sol Ultra в Work / облачном checkout
  └─ repository inventory, architecture, contracts, tests, static findings
  └─ безопасные hermetic-проверки
  └─ точная очередь функциональных runtime-сценариев для PHASE B

PHASE B — Sol Ultra через локальный Codex на целевой машине
  └─ продолжает тот же RUN_ID
  └─ проверяет Windows/Docker/WSL/GPU/LLM/GUI/runtime/persistence
  └─ подтверждает или опровергает выводы PHASE A
  └─ создаёт итоговую очередь задач для Spark

PHASE C — Codex Spark через локальный Codex
  └─ работает только по подтверждённым атомарным задачам
  └─ сначала воспроизводит, затем добавляет regression test и исправляет
```

PHASE A не имеет права разблокировать Spark.

---

## 1. Репозиторий и границы среды

Целевой репозиторий:

```text
Dest1k/jarvis-gpt
```

Найди фактический Git root текущего checkout и зафиксируй его. В облачной среде не предполагай Windows-путь.

На реальной машине используются разные каталоги:

```text
D:\jarvis-gpt   — Git-репозиторий и исходный код
D:\jarvis       — модели, Docker/runtime-данные, кеши, пользовательские данные,
                  большие логи и evidence
```

На PHASE A у тебя нет достоверного доступа к `D:\jarvis`. Поэтому нельзя объявлять подтверждёнными:

- запуск приложения;
- наличие моделей и образов;
- фактическое разрешение профиля в runtime;
- Docker/WSL/GPU behavior;
- GUI behavior;
- offline behavior;
- производительность;
- восстановление реальной БД или runtime state.

Такие выводы получают статус `RUNTIME_CONFIRMATION_REQUIRED`, `BLOCKED_BY_ENV` или `NOT_RUN`.

### Установка вне scope

Не проверяй первичную установку на чистую машину. Считай, что к PHASE B продукт уже установлен, необходимые assets получены, а базовый запуск в принципе возможен.

---

## 2. Обязательные правила

1. Не исправляй production-код, production-конфигурацию, штатные tests и lockfiles.
2. Разрешены только новые артефакты под `.audit/**` и временные harness-файлы внутри текущего audit run.
3. Не запускай Docker services, WSL workloads, реальную LLM, backend/frontend servers, browser automation, host bridge и внешние сетевые проверки.
4. Не обращайся к реальным аккаунтам, сайтам, локальным данным пользователя или внешним системам.
5. Запускай только изолированные проверки, которые работают на текущем checkout с fake/mock/synthetic data.
6. Можно устанавливать pinned development dependencies только в одноразовой облачной среде, не меняя tracked-файлы. Все команды и версии фиксируй.
7. Не записывай секреты, токены и приватные данные. Потенциально чувствительные значения маскируй.
8. Не используй destructive Git-команды, не переписывай историю и не сбрасывай чужие изменения.
9. Не называй runtime-дефект подтверждённым только по чтению кода.
10. Не останавливай весь аудит после первой ошибки: зафиксируй её и продолжай независимые направления.
11. При конфликте документации, tests, config и implementation создавай `SPEC_GAP`, а не угадывай.
12. Отчёт должен быть воспроизводимым: path, symbol, command, exit code, duration, evidence и связь с требованием.
13. Не публикуй exploit-ready детали. Для защитных findings достаточно boundary, impact, affected flow и безопасной рекомендации.
14. В финале сохрани audit artifacts в репозитории. Если Work поддерживает Git-изменения, создай отдельную audit-ветку и один или несколько audit-only commits. Не меняй default branch production-кодом.

---

## 3. Не навязывай устаревшую картину проекта

Сначала выведи фактическое состояние текущего commit. Не предполагай заранее:

- количество профилей и моделей;
- актуальность README;
- активность legacy-кода;
- доступность функции только потому, что найден её класс или endpoint;
- достаточность test coverage только потому, что test-файл существует.

Сопоставляй README, CLI help, launcher, `.env.example`, Compose, backend schemas, frontend labels и tests.

Известные ориентиры, которые надо проверить, а не принять на веру:

- Windows/PowerShell launcher;
- FastAPI backend, REST, streaming и WebSocket events;
- Next.js Command Center;
- локальный OpenAI-compatible LLM dispatcher;
- missions, planning, tools, approvals, memory, persona, learning и autonomy;
- filesystem, host, Docker, web/browser и document surfaces;
- ingestion и document workflows;
- SQLite/WAL persistent state;
- offline-first локальное ядро;
- тяжёлые данные вне Git;
- скрытый legacy-код не должен просачиваться в активный UX или routing без контракта.

Составь полный фактический каталог, а не ограничивайся этим seed-списком.

---

## 4. Идентификаторы и статусы

Используй стабильные ID:

```text
FEAT-...      функция/поверхность
REQ-...       требование
INV-...       инвариант
SCN-...       сценарий
TEST-...      проверка
EVID-...      evidence
JARVIS-....   finding
CTASK-....    candidate task для будущего Spark
```

### Статусы PHASE A

- `PASS_HERMETIC` — проверка реально выполнена в изолированном checkout и oracle подтверждён;
- `FAIL_HERMETIC` — проверка реально выполнена и контракт нарушен;
- `STATIC_SUPPORTED` — вывод подтверждён кодом, схемой или графом, но не является runtime PASS;
- `RUNTIME_CONFIRMATION_REQUIRED` — нужен живой runtime;
- `BLOCKED_BY_ENV` — облачная среда не позволяет выполнить проверку;
- `NOT_APPLICABLE` — доказуемо неприменимо;
- `NOT_RUN` — ещё не выполнено;
- `INCONCLUSIVE` — данные противоречивы.

### Статусы findings

- `static-confirmed`;
- `probable-runtime`;
- `spec-gap`;
- `test-gap`;
- `refuted`;
- `inconclusive`.

Для каждой выполненной команды сохраняй:

- UTC timestamp;
- exact command;
- working directory;
- tool/environment versions;
- exit code;
- duration;
- stdout/stderr path;
- связанные TEST/SCN IDs.

---

## 5. Единый run-каталог

Создай:

```text
.audit/
  LATEST_STATIC_RUN.txt
  runs/
    <RUN_ID>/
      START_HERE.md
      PIPELINE_STATE.json
      AUDIT_STATE.md
      SOURCE_BASELINE.json
      PHASE_A_COMPLETION.md
      STATIC_EXECUTIVE_SUMMARY.md
      SYSTEM_MAP.md
      FEATURE_CATALOG.md
      CONFIGURATION_MAP.md
      BEHAVIORAL_CONTRACT.md
      DATA_AND_TRUST_BOUNDARIES.md
      TEST_INVENTORY.md
      STATIC_TEST_RESULTS.md
      STATIC_COVERAGE_REPORT.md
      STATIC_SCENARIO_MATRIX.csv
      STATIC_FINDINGS_INDEX.md
      SPEC_GAPS.md
      TEST_GAPS.md
      ARCHITECTURE_RISKS.md
      LIVE_AUDIT_PLAN.md
      LIVE_SCENARIO_QUEUE.csv
      SOURCE_DRIFT_POLICY.md
      AUDIT_JOURNAL.md
      EVIDENCE_MANIFEST.json
      findings/
        JARVIS-0001.md
      evidence/
        static/
      harness/
        static/
      candidate_tasks/
        CTASK-0001.md
      handoff/
        PHASE_B_START_HERE.md
      machine/
        features.jsonl
        requirements.jsonl
        scenarios.jsonl
        findings.jsonl
        candidate_tasks.jsonl
```

`RUN_ID`:

```text
<UTC timestamp>_<short source commit SHA>
```

`.audit/LATEST_STATIC_RUN.txt` содержит только:

```text
.audit/runs/<RUN_ID>
```

Не создавай:

```text
.audit/LATEST_COMPLETE_RUN.txt
.audit/LATEST_RUN.txt
spark/READY
spark/safety/READY
```

Эти маркеры разрешены только после PHASE B.

### `PIPELINE_STATE.json`

Минимум:

```json
{
  "schema_version": 1,
  "run_id": "...",
  "repository": "Dest1k/jarvis-gpt",
  "source_commit": "full SHA before audit artifacts",
  "source_branch": "...",
  "phase_a": {
    "status": "IN_PROGRESS | COMPLETE | COMPLETE_WITH_BLOCKERS | INCOMPLETE",
    "executor": "Sol Ultra / Work",
    "live_machine_access": false
  },
  "phase_b": {"status": "NOT_STARTED"},
  "spark": {"status": "LOCKED_UNTIL_PHASE_B"},
  "canonical_local_paths": {
    "repo_root": "D:\\jarvis-gpt",
    "data_root": "D:\\jarvis"
  }
}
```

Зафиксируй `source_commit` до создания `.audit`.

---

## 6. Организация работы

Если доступны параллельные рабочие потоки, раздели read-only исследование:

1. inventory и architecture;
2. launcher/config/Compose;
3. backend/API/storage;
4. agent/LLM/tool orchestration/memory;
5. web/document/file surfaces;
6. frontend/GUI state по коду;
7. defensive design и sensitive-data handling;
8. tests/coverage/quality gates;
9. независимая проверка полноты и противоречий.

Главный агент обязан проверять первичные источники, дедуплицировать findings, сохранять точные ссылки на файлы/symbols и сериализовать запись `.audit`.

---

## 7. PHASE A0 — baseline

1. Найди Git root.
2. Зафиксируй remote, branch, full SHA, parent, tags, submodules и status.
3. Отдели tracked source от generated/vendor/cache/build artifacts.
4. Прочитай README, docs, repository instructions, schemas, examples и help-тексты.
5. Найди штатные test/lint/type/build/doctor/config commands.
6. Зафиксируй доступные версии Python, Node, package managers, PowerShell и test tools.
7. Создай `SOURCE_BASELINE.json` и журнал.
8. Проверяй, что production working tree не меняется.

---

## 8. PHASE A1 — полный индекс

Проиндексируй все tracked-файлы и классифицируй:

```text
production | test | config | script | documentation | schema |
migration | generated | vendored | legacy | unknown
```

Для значимых файлов укажи:

- path и роль;
- public entry points;
- подсистему;
- входящие/исходящие зависимости;
- persistence/network/process side effects;
- test coverage;
- был ли файл просмотрен;
- необходимость PHASE B.

Обязательно найди:

- CLI/launcher/PowerShell entry points;
- backend routes, middleware и dependencies;
- stream/WebSocket contracts;
- frontend pages/components/hooks/stores/service worker;
- profile/model/config resolution;
- Dockerfiles/Compose services/profiles/volumes/healthchecks;
- LLM providers, dispatchers и fallback paths;
- missions, tools, approvals и host actions;
- memory/persona/learning/autonomy/self-heal;
- web/browser/search/weather/watch/archive/feed/shop surfaces;
- document ingestion/recall/review/convert/edit paths;
- SQLite schema, migrations, WAL и indexes;
- background jobs, retries, queues, timers и supervisors;
- logging/audit/telemetry/benchmark paths;
- env precedence и feature flags;
- внешние библиотеки, system commands и network dependencies.

Каждая public feature, endpoint, event, CLI command, UI control, tool и profile получает `FEAT-...` либо статус `LEGACY/INACTIVE` с доказательством.

---

## 9. PHASE A2 — архитектура и данные

Создай `SYSTEM_MAP.md`:

- process/service topology;
- startup/shutdown flow;
- UI/CLI → API → agent → model/tools → storage → response;
- streaming/event path;
- profile/model resolution;
- approval and host-action flow;
- document/web ingestion flow;
- persistent-state lifecycle;
- background jobs;
- privilege and trust transitions;
- sensitive-data flow и redaction points;
- пути от недоверенного input к parser, model, tool, files и DB.

В `DATA_AND_TRUST_BOUNDARIES.md` для каждого типа данных укажи:

- source и уровень доверия;
- validation/canonicalization;
- storage;
- retention/deletion;
- logging exposure;
- session isolation;
- consumers/sinks;
- какие безопасные runtime checks нужны PHASE B.

---

## 10. PHASE A3 — поведенческий контракт

Собери контракт в порядке:

1. актуальные operator docs и явные требования;
2. public CLI/API/schema/UI promises;
3. tests;
4. configuration/defaults;
5. implementation.

При конфликте создавай `SPEC_GAP`.

Минимальные классы инвариантов:

- профиль выбирает предназначенную конфигурацию и модель;
- локальное ядро готового изделия не зависит от интернета, кроме явно сетевых функций;
- сетевой feature failure не ломает локальные функции;
- запросы и сессии не теряются, не дублируются и не смешиваются;
- cancel/retry не повторяет необратимые side effects;
- approval привязан к точному действию и аргументам;
- model/tool failure не отображается как успех;
- stream сохраняет порядок и terminal state;
- restart/crash не оставляет silent-corrupt state;
- repeated start/stop не накапливает orphan resources;
- чувствительные данные не попадают в лишние logs/UI/model context;
- недоверенный контент не получает управляющих привилегий;
- filesystem roots и path normalization соблюдаются;
- schema/version drift не приводит к silent data loss;
- frontend state соответствует backend truth;
- disabled/legacy feature не появляется в active routing или UI без контракта;
- nondeterministic LLM output оценивается по semantic contract, а не exact string.

Каждый `FEAT` получает `REQ/INV` либо `SPEC_GAP`.

---

## 11. PHASE A4 — tests и безопасные hermetic-проверки

Создай `TEST_INVENTORY.md`:

- unit/integration/E2E/smoke/manual tests;
- fixtures/mocks/fakes;
- network/Docker/model/host dependencies;
- skipped/xfail/disabled/flaky markers;
- реальные assertions;
- feature/requirement coverage;
- tests, которые могут проходить без meaningful oracle;
- недостающие positive/error/recovery tests.

Запусти все доступные repository-only проверки, которые не требуют живого runtime:

- Python syntax/compile/import;
- unit/integration suites с fake/mocked dependencies;
- lint/type checks;
- API/schema/OpenAPI consistency;
- frontend lint/typecheck/unit/build в sandbox;
- PowerShell parser/static analysis, если доступно;
- YAML/JSON/TOML/env/Compose static rendering;
- generated schema/client drift;
- coverage ключевых модулей;
- deterministic CLI checks без запуска сервисов.

Перед каждой командой проверь, что она не стартует Docker/WSL/services, не обращается к real LLM, не пишет в `D:\jarvis`, не читает пользовательскую БД и не открывает внешнюю сеть. Такие tests переносятся в `LIVE_SCENARIO_QUEUE.csv`.

Для оценки качества tests используй безопасные методы: review assertions, временные локальные изменения в копии, boundary/property checks чистых функций. Не меняй tracked source.

---

## 12. PHASE A5 — глубокий review по подсистемам

### 12.1 Launcher, profiles, env и Compose

Проверь:

- quoting, escaping и path handling;
- запуск из другого cwd;
- пробелы, кириллицу и длинные пути;
- env precedence, пустые и malformed values;
- aliases/defaults/unknown profile;
- model/profile mismatch;
- конфликтующие config sources;
- возможный неожиданный pull/build/download/metadata lookup;
- offline policy и image pinning;
- startup ordering и health assumptions;
- stale PID/lock/container handling;
- idempotency start/stop/restart;
- process cleanup;
- слишком широкие Docker/WSL operations;
- inactive/legacy names в active paths;
- качество диагностики.

### 12.2 Backend, API, streaming и WebSocket

Проверь:

- schemas и drift;
- missing/extra/wrong/oversized fields;
- bind/origin/CORS/auth assumptions;
- exception handling и silent success;
- cancellation propagation;
- reconnect lifecycle;
- ordering, duplicate/late/missing terminal events;
- slow consumers и backpressure;
- idempotency/retry;
- async task leaks/races/deadlocks;
- resource cleanup;
- correlation IDs, logging и redaction;
- persisted state transitions.

### 12.3 Agent, LLM/provider, missions и tools

Проверь:

- intent routing и deterministic shortcuts;
- planner/executor disagreement;
- loop/step budgets;
- malformed/partial/duplicate tool calls;
- invalid tool names/args/types;
- permission and approval binding;
- retries around side effects;
- cancellation timing;
- huge or unusual tool output;
- untrusted content entering model/tool context;
- context truncation и evidence loss;
- false-success paths;
- provider-unavailable fallback;
- cross-session isolation;
- mission resume/final report integrity;
- self-verification loops;
- persona/learning data quality;
- autonomy/self-heal boundaries.

### 12.4 Storage, memory, persona и jobs

Проверь:

- SQLite migrations/schema versioning;
- transactions, WAL, locks и retries;
- partial writes и atomicity;
- duplicate events/idempotency;
- corruption detection/recovery design;
- cleanup/retention/size bounds;
- session isolation;
- stale indexes/cache invalidation;
- memory/persona dedup and quality controls;
- background-job concurrency;
- timezone/clock handling;
- serialization safety;
- sensitive-data retention.

### 12.5 Web/browser/document/file surfaces

Проверь защитный дизайн:

- URL validation и destination restrictions;
- redirect/scheme handling;
- download quarantine и type detection;
- isolation of untrusted page/document content;
- source attribution and provenance;
- cache freshness labeling;
- parser/render fallback;
- browser policy/approval/session isolation;
- timeout/cancel/recovery;
- external-content size limits;
- allowed roots and path normalization;
- filename/extension/MIME inconsistencies;
- oversized input handling;
- parser failure isolation;
- copy-on-write and original preservation;
- output collision handling;
- Unicode/encoding;
- chunk/index consistency;
- cleanup and retention.

Не выполняй active network/security tests в PHASE A.

### 12.6 Frontend и GUI state по коду

Проверь:

- API/schema drift;
- stale closures/unhandled promises;
- duplicate submit/events;
- stream cancel/reconnect;
- stale optimistic success;
- error/degraded states;
- history restore and session state;
- keyboard/focus/accessibility semantics;
- long text/code/table/URL layout assumptions;
- resize/DPI assumptions;
- service-worker cache invalidation;
- safe rendering of untrusted text;
- sensitive-data exposure;
- profile/model/status display;
- hidden legacy labels.

Не объявляй визуальный PASS без PHASE B.

### 12.7 Defensive design и dependencies

Проверь на уровне architecture/code review:

- validation before executing commands/actions;
- file-root boundaries;
- sensitive-data handling;
- local API exposure and defaults;
- least privilege for tools and approvals;
- safe deserialization and schema enforcement;
- network destination restrictions;
- dependency/image pinning;
- install hooks and reproducibility;
- credentials accidentally committed;
- log/control-character handling;
- retry behavior for state-changing actions.

Любую активную проверку оставь PHASE B и опиши безопасно, без operational payloads.

### 12.8 Performance/resource risks по коду

Найди:

- unbounded queues/history/logs/cache;
- repeated full scans и очевидные complexity hot spots;
- blocking I/O in async paths;
- runaway retries/timers;
- leaked tasks/processes/files/sockets;
- large copies/serialization;
- missing pagination/backpressure;
- expensive startup work;
- GPU/model lifecycle assumptions;
- frontend render storms;
- storage growth without retention.

Численные измерения оставь PHASE B.

---

## 13. PHASE A6 — поиск скрытых ошибок безопасными методами

Где разумно, используй:

- property-based tests чистых parsers/state functions;
- schema-aware generation только для локальных test interfaces;
- model-based tests детерминированных state machines;
- metamorphic tests эквивалентных inputs/config order;
- differential checks implementations/fallbacks;
- seeded random sequences на fake runtime;
- call graph/data flow review;
- TODO/FIXME/HACK/dead/legacy branch review;
- exception-path inventory;
- negative-space review: docs обещают больше, чем code/tests.

Используй строгие budgets и watchdogs. Никакой внешней сети и active security testing.

---

## 14. PHASE A7 — scenario matrices и handoff

`STATIC_SCENARIO_MATRIX.csv` минимум:

```text
scenario_id,feature_ids,requirement_ids,domain,component,method,
preconditions,state_before,stimulus,oracle,invariants,profile,
network_state,resource_state,storage_state,concurrency,permissions,
locale_encoding,phase,status,evidence_ids,finding_ids,notes
```

`LIVE_SCENARIO_QUEUE.csv` минимум:

```text
order,scenario_id,priority,risk,domain,feature_ids,requirement_ids,
exact_preconditions,exact_commands_or_actions,required_profile,
required_services,state_setup,stimulus,expected_oracle,
telemetry_to_capture,safety_isolation,cleanup,repeat_count,
time_budget,source_findings,dependencies,status,notes
```

Live-сценарии должны быть функциональными и безопасными. Они могут охватывать:

- все фактические profiles;
- stopped/starting/ready/busy/degraded/recovering;
- online/offline/intermittent connectivity;
- model ready/loading/slow/error/disconnected;
- API/stream/WebSocket normal/slow/disconnected/reconnected;
- empty/large/Unicode/code/JSON/malformed inputs;
- tool success/failure/timeout/cancel/partial/retry;
- multi-session/concurrent/cancel+new request;
- storage normal/locked/read-only/nearly-full/copy-based recovery;
- paths с пробелами/кириллицей;
- ordinary/denied permissions;
- clock/timeout boundaries;
- GUI sizes/DPI/stream/error/long-history states.

Для destructive/corruption scenarios требуй копии, synthetic roots и cleanup. Не включай exploit payloads или действия против внешних систем.

---

## 15. Формат finding

Каждый `findings/JARVIS-NNNN.md`:

```yaml
id: JARVIS-0001
title: "..."
kind: defect | reliability | data-integrity | performance | ux | defensive-design | spec-gap | test-gap
severity: critical | high | medium | low
priority: P0 | P1 | P2 | P3
phase_a_status: static-confirmed | probable-runtime | spec-gap | test-gap | refuted | inconclusive
confidence: 0.00-1.00
reproducibility: hermetic-always | hermetic-intermittent | static-proof | runtime-required
components: []
feature_ids: []
requirement_ids: []
scenario_ids: []
evidence_ids: []
affected_paths: []
phase_b_scenarios: []
candidate_task_ids: []
```

Разделы:

1. Summary.
2. Contract and impact.
3. Static evidence with exact paths/symbols.
4. Hermetic reproduction, если есть.
5. Expected vs observed.
6. Why runtime confirmation is or is not required.
7. Root cause: confirmed / hypothesis / unknown.
8. Affected flow.
9. Data-integrity, privacy or permission implications, если применимо.
10. Exact safe PHASE B confirmation/refutation procedure.
11. Suggested remediation direction без production fix.
12. Regression risks.
13. Acceptance criteria draft.
14. Related findings/spec/test gaps.

Дедуплицируй findings по root cause и не завышай severity.

---

## 16. Candidate tasks для Spark

Можно создать `candidate_tasks/CTASK-NNNN.md`, но:

- status только `AWAITING_PHASE_B`, `BLOCKED_BY_SPEC` или `STATIC_ONLY_REVIEW_REQUIRED`;
- не помещай их в финальную Spark queue;
- не создавай READY markers;
- укажи runtime checks до READY;
- одна candidate task = одна root cause/одна подсистема;
- перечисли context files, tentative allowed files, regression test и acceptance criteria;
- не перекладывай на Spark архитектурное решение без контракта.

---

## 17. Handoff для PHASE B

Создай `handoff/PHASE_B_START_HERE.md`:

1. run path и source commit;
2. что выполнила PHASE A;
3. реальные commands/suites;
4. counts результатов;
5. static-confirmed findings;
6. probable-runtime findings;
7. top spec/test gaps;
8. порядок `LIVE_SCENARIO_QUEUE.csv`;
9. prerequisites;
10. safety/isolation notes;
11. source drift policy;
12. какие artifacts PHASE B обновляет, а какие сохраняет;
13. критерии завершения объединённого аудита;
14. явный запрет Spark READY до конца PHASE B.

### Source drift

PHASE B сравнивает production tree с `source_commit`, исключая:

```text
.audit/**
docs/audit/**
```

При drift она создаёт `SOURCE_DRIFT.md`, перечитывает затронутые files, повторяет релевантные checks и не переносит старые conclusions автоматически.

---

## 18. Traceability consistency

Поддерживай:

```text
feature -> requirement/invariant -> scenario/test -> result -> evidence
        -> finding -> live scenario -> candidate task
```

Создай consistency harness, который обнаруживает:

- unknown/duplicate IDs;
- feature без contract/spec-gap;
- high-risk requirement без positive/error/recovery scenario;
- FAIL без finding;
- finding без evidence;
- probable-runtime finding без PHASE B scenario;
- candidate task без finding;
- runtime scenario, ошибочно отмеченный PASS на PHASE A;
- broken paths/references.

Запусти его перед завершением.

---

## 19. Критерии завершения PHASE A

Не ставь `phase_a.status = COMPLETE`, пока:

1. проиндексированы tracked source/config/test/script/doc files;
2. построена карта компонентов, entry points, data stores и trust boundaries;
3. public features получили IDs;
4. features связаны с contracts/invariants либо SPEC-GAP;
5. tests инвентаризированы, а безопасные hermetic checks выполнены или точно заблокированы;
6. создан coverage/gap report;
7. выполнен review всех подсистем;
8. использованы безопасные методы поиска скрытых ошибок;
9. findings имеют evidence и честный статус;
10. probable-runtime findings имеют live scenarios;
11. `LIVE_SCENARIO_QUEUE.csv` имеет safety, cleanup и oracles;
12. создан PHASE B handoff;
13. consistency harness PASS;
14. production-код не изменён;
15. не созданы combined-audit или Spark READY markers.

`COMPLETE_WITH_BLOCKERS` допустим, если вся доступная работа закончена, а блокеры документированы. `INCOMPLETE` — если работа оборвалась.

---

## 20. Сохранение результата

Предпочтительный вариант:

1. создать branch `audit/phase-a-<RUN_ID>` от зафиксированного source commit;
2. добавить только `.audit/**`;
3. проверить, что production files не изменены;
4. создать audit-only commit;
5. не merge и не push в другие ветки сверх возможностей Work;
6. в финальном ответе указать branch, commit и способ забрать результат.

Если Work может коммитить только в предоставленную ветку, используй её, но stage только `.audit/**`. Если запись в Git недоступна, сохрани полный набор artifacts доступным способом и явно укажи blocker.

---

## 21. Финальный ответ Work

Не вставляй огромный отчёт в чат. Сначала сохрани artifacts. Затем кратко сообщи:

- путь из `.audit/LATEST_STATIC_RUN.txt`;
- source commit и branch;
- audit artifact branch/commit;
- counts features/requirements/scenarios/tests;
- результаты hermetic checks;
- findings по severity/status;
- крупнейшие SPEC/TEST gaps;
- количество live-сценариев;
- путь к `handoff/PHASE_B_START_HERE.md`;
- явную фразу, что runtime ещё не проверен и Spark заблокирован.

Красивое резюме без file inventory, выполненных hermetic checks, evidence, traceability и исполнимого PHASE B handoff считается незавершённой PHASE A.
