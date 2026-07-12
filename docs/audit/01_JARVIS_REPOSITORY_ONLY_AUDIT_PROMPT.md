# JARVIS — PHASE A: ПОЛНЫЙ АУДИТ РЕПОЗИТОРИЯ БЕЗ ДОСТУПА К ЖИВОЙ МАШИНЕ

Ты работаешь как главный инженер по качеству, системный архитектор, специалист по надёжности и безопасности, adversarial reviewer и координатор независимых субагентов. Выполни **первую половину двухкомпонентного доказательного аудита JARVIS**: максимально глубокое исследование репозитория, которое возможно без доступа к целевой Windows-машине, её Docker/WSL/GPU, локальным моделям, пользовательским данным и реально запущенному приложению.

Это не поверхностное code review. Твоя задача — исчерпывающе разобрать кодовую базу, контракты, конфигурацию, тесты, переходы состояний, доверительные границы и потенциальные классы отказов; выполнить все безопасные и воспроизводимые проверки, которые можно провести в изолированном checkout; затем сохранить в репозитории строгий handoff для второй фазы, которая будет запущена на живой машине через Codex Sol Ultra.

После второй фазы итоговый аудит должен превратиться в атомарную очередь исправлений для Codex Spark. **На этой фазе не выпускай финальную READY-очередь для Spark:** runtime-гипотезы ещё не подтверждены. Подготовь кандидатов и всю необходимую контекстную базу, но оставь Spark заблокированным до завершения PHASE B.

---

## 0. Контракт всей трёхступенчатой кампании

Кампания состоит из трёх последовательных исполнителей:

```text
PHASE A — этот prompt, Sol Ultra в Work / облачном checkout
  └─ статический, контрактный и hermetic-аудит репозитория
  └─ карта системы, инварианты, тестовый инвентарь, findings
  └─ точная очередь runtime-сценариев для живой машины

PHASE B — Sol Ultra через локальный Codex на целевой машине
  └─ продолжает тот же RUN_ID
  └─ проверяет реальное поведение Windows/Docker/WSL/GPU/LLM/GUI
  └─ подтверждает или опровергает findings PHASE A
  └─ создаёт финальную очередь задач для Spark

PHASE C — Codex Spark через локальный Codex
  └─ читает только завершённый объединённый аудит
  └─ чинит по одной атомарной задаче
  └─ добавляет regression tests и повторно валидирует результат
```

Твоя работа обязана быть пригодной для бесшовного продолжения PHASE B без доступа к твоему контексту, скрытым рассуждениям или сообщениям чата.

---

## 1. Репозиторий и границы среды

Целевой репозиторий: `Dest1k/jarvis-gpt`.

В Work/облачной среде путь checkout заранее неизвестен. Найди фактический корень командой:

```bash
git rev-parse --show-toplevel
```

Работай только внутри найденного Git-корня. Не требуй наличия Windows-пути и не создавай фиктивные каталоги диска `D:`.

На реальной машине позднее используются два разных корня:

```text
D:\jarvis-gpt   — рабочий Git-репозиторий и исходный код
D:\jarvis       — тяжёлые runtime-данные, модели, Docker-данные, кеши,
                  пользовательские данные, большие логи и evidence
```

На PHASE A:

- у тебя **нет** достоверного доступа к `D:\jarvis-gpt` как к локальному Windows checkout;
- у тебя **нет** доступа к содержимому `D:\jarvis`;
- ты не можешь подтверждать наличие моделей, образов, volumes, портов, GPU, драйверов, WSL или работающих сервисов;
- не имитируй такую доступность и не объявляй runtime-сценарии пройденными по чтению кода;
- любые выводы о живой системе должны получить статус `RUNTIME_CONFIRMATION_REQUIRED`, `NOT_RUN` либо `BLOCKED_BY_ENV`.

### Установка исключена из аудита

Не проверяй и не проектируй сценарии первичной установки на чистую машину. Считай, что к PHASE B продукт уже установлен, модели и необходимые образы каким-то образом получены, а базовый запуск в принципе возможен.

В scope остаются только свойства готового изделия: запуск из подготовленного состояния, повторный запуск, конфигурация, runtime, деградация, восстановление, безопасность, целостность, UX и длительная эксплуатация.

---

## 2. Жёсткие правила

1. **Не изменяй production-код, production-конфигурацию, lockfiles и штатные тесты ради исправления найденных дефектов.** Объект аудита должен оставаться неизменным.
2. Разрешены только артефакты под `.audit/`, изолированные harness/repro-файлы под текущим run-каталогом и временные файлы вне tracked source.
3. Не запускай Docker daemon, `docker compose up`, WSL, GPU workloads, vLLM, реальную модель, локальные backend/frontend services, browser automation против внешних сайтов или host-level команды.
4. Не выполняй внешние действия, которые могут затронуть реальные аккаунты, сайты, машины или данные.
5. Репозиторные тесты разрешены только тогда, когда они полностью изолированы от живой машины и не требуют реальных моделей, host bridge, Docker runtime, пользовательской БД или внешней сети.
6. Можно установить pinned development dependencies **только внутри одноразовой облачной среды**, если это не меняет tracked-файлы и необходимо для hermetic-тестов. Зафиксируй точные команды и версии. Невозможность установки — `BLOCKED_BY_ENV`, а не дефект JARVIS.
7. Можно выполнять compile/lint/type/schema/unit/property/fuzz/mutation checks в sandbox с жёсткими лимитами времени, памяти и диска.
8. Можно выполнять `docker compose config` или эквивалентный статический рендер конфигурации только без запуска сервисов, pull образов и обращения к host runtime; используй синтетические не секретные env-значения.
9. Не записывай секреты, токены или приватные данные в аудит. Любое найденное потенциальное значение маскируй.
10. Не используй `git reset --hard`, `git clean`, массовое удаление или перезапись пользовательских изменений.
11. Не выдавай подозрительный код за подтверждённый runtime-дефект. Строго разделяй доказанное статически, подтверждённое hermetic-тестом, вероятное и требующее живого воспроизведения.
12. Не останавливай весь аудит после первого красного теста. Зафиксируй результат и продолжай независимые направления.
13. Не спрашивай пользователя о каждом неоднозначном месте. Извлекай контракт из доступных источников; при конфликте создавай `SPEC_GAP`.
14. В финале сохрани все артефакты в Git checkout. Если среда Work поддерживает commit/publish — создай один commit только с `.audit/**`; не меняй production-файлы и не делай merge. Если commit недоступен, оставь полный diff и явно сообщи это.

---

## 3. Не навязывай проекту устаревшую картину

Сначала выведи фактическое состояние текущего commit. Не предполагай заранее:

- точное количество runtime-профилей;
- точный список моделей;
- что все пункты старого README всё ещё верны;
- что внутреннее legacy-имя автоматически является пользовательским дефектом;
- что наличие кода означает доступность функции;
- что тест существует и действительно проверяет заявленное поведение.

Особенно внимательно сопоставь README, CLI help, launcher, `.env.example`, Compose, backend schemas, frontend labels и tests. Если, например, один источник говорит о двух профилях, а другой — о трёх, это не повод молча выбрать удобный вариант: создай SPEC-GAP и подготовь живую проверку фактического разрешения профилей.

Известные ориентиры, которые надо проверить, а не принять на веру:

- Windows + PowerShell + Docker/WSL2;
- FastAPI backend, REST API, NDJSON streaming и WebSocket events;
- Next.js Command Center;
- локальный OpenAI-compatible LLM dispatcher/vLLM;
- missions, planning, tool loop, approvals, memory, persona, learning и autonomy;
- filesystem, host bridge/Windows native, Docker, browser/web и document tools;
- ingestion Word/Excel/PDF/PPTX/text и document-surfer workflows;
- SQLite/WAL persistent state;
- offline-first локальное ядро с корректной деградацией сетевых функций;
- тяжёлые данные вне Git-репозитория;
- скрытый legacy-код не должен неожиданно просачиваться в пользовательские тексты или active routing.

Составь полный фактический каталог; этот список является только seed.

---

## 4. Идентификаторы, статусы и доказательная дисциплина

Используй стабильные ID:

```text
FEAT-...      функция/поверхность
REQ-...       требование
INV-...       инвариант
SCN-...       сценарий
TEST-...      тест/проверка
EVID-...      evidence
JARVIS-....   finding
CTASK-....    candidate task для будущего Spark
```

### Статусы сценариев на PHASE A

Разрешены только:

- `PASS_HERMETIC` — реально выполнен в изолированном checkout и oracle проверен;
- `FAIL_HERMETIC` — реально выполнен и нарушил контракт;
- `STATIC_SUPPORTED` — вывод доказан кодом/схемой/графом, но не является runtime-прохождением;
- `RUNTIME_CONFIRMATION_REQUIRED` — статический сигнал требует живой машины;
- `BLOCKED_BY_ENV` — облачная среда не позволяет выполнить проверку;
- `NOT_APPLICABLE` — доказуемо неприменимо;
- `NOT_RUN` — ещё не выполнено;
- `INCONCLUSIVE` — данные противоречивы.

На PHASE A **не используй обычный `PASS` для runtime-сценариев**.

### Статусы findings

- `static-confirmed` — дефект однозначно следует из контракта и кода либо воспроизводится hermetic-тестом;
- `probable-runtime` — сильная гипотеза, которую должна проверить PHASE B;
- `spec-gap` — желаемое поведение неоднозначно;
- `test-gap` — опасная область не имеет достаточного oracle/coverage;
- `refuted` — первоначальная гипотеза опровергнута в рамках PHASE A;
- `inconclusive` — доказательств недостаточно.

Для каждой выполненной команды сохраняй:

- UTC timestamp;
- exact command;
- working directory;
- environment/tool versions;
- exit code;
- duration;
- stdout/stderr path;
- связанные TEST/SCN IDs;
- созданные или изменённые временные файлы.

Для fuzz/property/model-based тестов сохраняй seed, budget, минимизированный input и команду воспроизведения.

---

## 5. Создай единый run и handoff

В корне репозитория создай:

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

Пример:

```text
20260712T230500Z_748a892
```

В `.audit/LATEST_STATIC_RUN.txt` должна быть только относительная строка:

```text
.audit/runs/<RUN_ID>
```

**Не создавай** `.audit/LATEST_COMPLETE_RUN.txt`, `.audit/LATEST_RUN.txt` и `spark/READY`: эти маркеры принадлежат только завершённой PHASE B. Это защищает пользователя от преждевременного запуска Spark по частичному аудиту.

### Минимальная схема `PIPELINE_STATE.json`

```json
{
  "schema_version": 1,
  "run_id": "...",
  "repository": "Dest1k/jarvis-gpt",
  "source_commit": "full SHA audited before audit artifacts",
  "source_branch": "...",
  "phase_a": {
    "status": "IN_PROGRESS | COMPLETE | COMPLETE_WITH_BLOCKERS | INCOMPLETE",
    "started_at_utc": "...",
    "finished_at_utc": null,
    "executor": "Sol Ultra / Work",
    "live_machine_access": false
  },
  "phase_b": {
    "status": "NOT_STARTED"
  },
  "spark": {
    "status": "LOCKED_UNTIL_PHASE_B"
  },
  "canonical_local_paths": {
    "repo_root": "D:\\jarvis-gpt",
    "data_root": "D:\\jarvis"
  }
}
```

Зафиксируй `source_commit` **до** создания `.audit`. Он обозначает именно версию production-кода, а не будущий commit с отчётами.

---

## 6. Организация субагентов и устойчивость длинной работы

Если доступны Ultra/subagents, раздели read-only работу на независимые направления:

1. repository inventory и architecture map;
2. launcher/PowerShell/config/Compose;
3. backend/API/WebSocket/storage;
4. agent core/LLM/tool loop/memory/persona/learning;
5. web/browser/document surfaces;
6. frontend/GUI state machine и accessibility по коду;
7. security/privacy/trust boundaries;
8. tests/coverage/property/fuzz/mutation;
9. независимый adversarial reviewer, который ищет пропуски и ложные выводы.

Главный агент обязан:

- проверить первичные источники каждого вывода;
- дедуплицировать findings по root cause;
- не принимать краткий отчёт субагента без ссылок на файлы/symbols/evidence;
- сериализовать изменения `.audit`, чтобы субагенты не перезаписывали друг друга;
- постоянно обновлять `AUDIT_STATE.md` и `AUDIT_JOURNAL.md`;
- после compaction перечитать `PIPELINE_STATE.json`, `AUDIT_STATE.md` и текущий workstream.

---

## 7. PHASE A0 — preflight и источник истины

1. Найди Git root.
2. Зафиксируй remote, branch, полный commit SHA, parent SHA, tags, submodules и `git status`.
3. Отдели tracked source от generated/vendor/cache/build artifacts.
4. Прочитай README, CONTRIBUTING, AGENTS/CLAUDE/CODEX-инструкции, docs, help-тексты, schemas, examples и comments, но не считай документацию автоматически истинной.
5. Найди штатные команды test/lint/type/build/doctor/compose-config.
6. Зафиксируй доступные версии Python, Node, npm/pnpm/yarn, PowerShell (если есть), Docker CLI (только версия; daemon не использовать), linters и test runners.
7. Создай `SOURCE_BASELINE.json` и первые строки журнала.
8. Проверь, что production working tree не изменяется в процессе аудита. Любое случайное изменение немедленно откати только для созданного тобой файла и задокументируй.

---

## 8. PHASE A1 — полный индекс репозитория

Построй полный индекс tracked-файлов с классификацией:

```text
production | test | config | script | documentation | schema |
migration | generated | vendored | legacy | unknown
```

Для каждого значимого файла укажи:

- path;
- язык/формат;
- роль;
- публичные entry points;
- владельца подсистемы;
- входящие/исходящие зависимости;
- persistence/network/process side effects;
- тестовое покрытие;
- был ли файл реально просмотрен;
- риск и необходимость PHASE B.

Найди и перечисли:

- CLI/launcher/PowerShell entry points;
- backend apps/routes/dependencies/middleware;
- WebSocket и streaming contracts;
- frontend pages/components/hooks/stores/service worker;
- profile/model/config resolution;
- Dockerfiles/Compose profiles/services/networks/volumes/healthchecks;
- LLM providers, dispatchers, fallback paths, token/stream processing;
- planners, missions, tools, approvals и host actions;
- memory/persona/experience/learning/autonomy/self-heal;
- web/browser/search/weather/watch/archive/feed/shop surfaces;
- document ingestion/recall/review/convert/edit paths;
- SQLite schema, migrations, WAL, cache/index lifecycle;
- background jobs, retries, queues, timers и supervisors;
- logging/audit/telemetry/benchmark/evidence paths;
- feature flags и env precedence;
- внешние библиотеки, system commands и network destinations.

Каждая публичная функция, endpoint, event, CLI command, UI control, tool и profile должна получить `FEAT-...` либо явную запись `LEGACY/INACTIVE` с доказательством.

---

## 9. PHASE A2 — карта архитектуры, данных и доверительных границ

Создай `SYSTEM_MAP.md` с диаграммами и точными file/symbol references:

- process/service topology;
- startup/shutdown/control flow;
- request path: UI/CLI → API → agent → model/tools → storage → response;
- streaming/event path;
- model/profile resolution path;
- approval path;
- host bridge/native action path;
- web/document ingestion path;
- persistent state lifecycle;
- background jobs/supervisor;
- trust boundaries и privilege transitions;
- secret flow и redaction points;
- paths from untrusted content into prompts, tools, shell, browser, files and DB.

В `DATA_AND_TRUST_BOUNDARIES.md` перечисли для каждого типа данных:

- source и trust level;
- validation/canonicalization;
- storage location;
- retention/deletion;
- logging/audit exposure;
- cross-session isolation;
- sinks и dangerous interpreters;
- PHASE B tests.

---

## 10. PHASE A3 — поведенческий контракт и инварианты

Собери контракт из текущих источников в порядке:

1. явные требования текущего репозитория и актуальные operator docs;
2. public CLI/API/schema/UI promises;
3. tests;
4. configuration/defaults;
5. implementation.

При конфликте не угадывай — создай SPEC-GAP с точными цитатами и путями.

Минимальные классы инвариантов:

- профиль выбирает только предназначенную ему конфигурацию/модель;
- локальное ядро готового изделия не требует интернета, кроме явно сетевых функций;
- сетевой feature failure не ломает локальный chat/runtime и объясняется пользователю;
- один запрос/сессия не теряется, не дублируется и не смешивается с другой;
- cancel/retry не повторяет необратимые side effects;
- approval привязан к точному действию и аргументам, не расширяется неявно;
- model/tool failure не отображается как успех;
- stream сохраняет порядок и имеет terminal state;
- restart/crash не оставляет silent-corrupt persistent state;
- repeated start/stop не накапливает orphan resources;
- secrets/PII не попадают в logs/UI/model context/audit;
- untrusted web/document/tool output не получает управляющих привилегий;
- filesystem roots и path normalization не обходятся traversal/junction/symlink tricks;
- schema/version drift не приводит к silent data loss;
- frontend state соответствует backend truth;
- disabled/legacy feature не появляется в active routing или UI без контракта;
- nondeterministic текст LLM оценивается по семантическому контракту, а не exact string.

Каждый `FEAT` должен иметь `REQ/INV` либо `SPEC-GAP`.

---

## 11. PHASE A4 — инвентаризация и hermetic-запуск тестов

Создай `TEST_INVENTORY.md`:

- unit/integration/E2E/smoke/manual tests;
- fixtures/mocks/fakes;
- network/Docker/model/host dependencies;
- skipped/xfail/disabled/flaky markers;
- фактические assertions;
- feature/requirement coverage;
- тесты, которые могут проходить без проверки результата;
- offline reproducibility;
- недостающие negative/recovery tests.

Выполни все доступные repository-only проверки, например, если они предусмотрены проектом:

- Python syntax/compile/import checks;
- unit/integration suites с fake/mocked dependencies;
- ruff/flake/mypy/pyright либо фактические эквиваленты;
- API/schema/OpenAPI consistency;
- frontend lint/typecheck/unit tests/build в sandbox;
- PowerShell parser/static analysis, если инструмент доступен;
- Compose/YAML/JSON/TOML/env static rendering/validation;
- generated schema/client drift checks;
- line/branch coverage ключевых модулей;
- deterministic smoke CLI, который не поднимает сервисы и не обращается к host runtime.

Перед каждым test command определи, не пытается ли он:

- стартовать Docker/WSL/services;
- обратиться к реальной LLM;
- писать в `D:\jarvis`;
- читать пользовательскую БД;
- открывать браузер или сеть;
- выполнять host commands.

Если пытается — не запускай в PHASE A; перенеси в `LIVE_SCENARIO_QUEUE.csv`.

Проверь качество тестов через выборочный mutation testing или ручные безопасные мутации в временной копии. Не изменяй tracked source. Докажи, что критичные tests действительно краснеют при нарушении инварианта.

---

## 12. PHASE A5 — глубокий статический аудит по подсистемам

### 12.1. Launcher, PowerShell, profiles, env и Compose

Проверь:

- quoting/escaping/path handling;
- запуск из другого cwd;
- пробелы, кириллица, длинные пути;
- env precedence и empty/malformed values;
- profile aliases, defaults, unknown profile;
- model/profile mismatch;
- duplicate/conflicting configuration sources;
- accidental pull/build/download/registry/package resolution;
- offline policy и image tags/digests;
- startup ordering/healthcheck assumptions;
- stale PID/lock/container detection;
- idempotency start/stop/restart;
- process cleanup;
- dangerous broad Docker/WSL commands;
- accidental use of inactive/legacy model names;
- diagnostics that hide root cause.

Всё, что зависит от реального resolved Compose/runtime, оформи как точный PHASE B scenario.

### 12.2. Backend, API, streaming и WebSocket

Проверь:

- input/output schemas и drift;
- validation of missing/extra/wrong/oversized fields;
- auth/origin/bind/CORS assumptions;
- exception handling и silent success;
- cancellation propagation;
- disconnect/reconnect lifecycle;
- ordering, duplicate/late/missing terminal events;
- backpressure/slow consumer;
- idempotency/retry;
- async task leaks/races/deadlocks;
- resource cleanup;
- correlation IDs/logging/redaction;
- error mapping machine-readable ↔ user-facing;
- route/tool registration consistency;
- persisted conversation/mission state transitions.

### 12.3. Agent core, LLM/provider, missions и tools

Проверь:

- intent routing, deterministic shortcuts и model arbitration;
- planner/executor disagreement;
- loop/step budgets;
- malformed/partial/duplicate tool calls;
- invalid tool names/args/types;
- tool permission and approval binding;
- retries around side effects;
- cancellation before/during/after side effect;
- huge/binary-like/escape-sequence tool output;
- prompt injection from user/file/web/tool/memory;
- context truncation and evidence loss;
- hallucinated success paths;
- fallback behavior without provider;
- cross-session leakage;
- mission resume/final report integrity;
- self-verification/rewrite loops;
- learning/persona feedback poisoning;
- autonomy/self-heal safety boundaries.

### 12.4. Storage, memory, persona, learning и jobs

Проверь:

- SQLite migrations/schema versioning;
- transaction boundaries, WAL, locks, retries;
- partial writes и atomicity;
- duplicate events/idempotency keys;
- corruption detection/recovery paths;
- cleanup/retention/size bounds;
- session isolation;
- stale indexes/cache invalidation;
- memory/persona dedup and poisoning;
- concurrency between supervisor/jobs/UI/API;
- timezone/clock jump behavior;
- unsafe serialization/deserialization;
- secret/PII retention and deletion semantics.

### 12.5. Web, browser, shopping, weather, watch и evidence

Проверь:

- SSRF/url validation/DNS rebinding assumptions;
- redirects, schemes, credentials-in-URL, localhost/private ranges;
- download quarantine, archive paths, MIME confusion;
- prompt injection from fetched content;
- source attribution/citation/provenance integrity;
- cache freshness and stale-result labeling;
- parser/render fallback correctness;
- browser policy/approval binding/multi-tab isolation;
- timeout/cancel/retry;
- external content size limits;
- log/terminal escape injection;
- deterministic named-shop routing and constraints;
- online-only functions degrading without breaking local core.

Не выполняй реальные external requests в PHASE A; подготовь mocks/hermetic tests и live scenarios.

### 12.6. Documents и file ingestion

Проверь:

- allowed roots/path normalization;
- traversal/junction/symlink/archive-slip;
- malicious/ambiguous filenames;
- oversized files/decompression bombs;
- MIME/extension mismatch;
- parser failure isolation;
- macro/external-link behavior;
- formula and spreadsheet edge cases;
- PDF/OCR fallback semantics;
- copy-on-write guarantee;
- original-file preservation;
- output collisions;
- encoding/Unicode;
- chunk/index consistency;
- prompt injection in document content;
- persisted source provenance;
- safe cleanup and retention.

### 12.7. Frontend, service worker и GUI state machine по коду

Статически проверь:

- API/schema drift;
- stale closures/unhandled promises;
- duplicate submit/events;
- stream cancel/reconnect;
- stale optimistic success;
- error and degraded states;
- history restore and cross-session state;
- focus/keyboard/accessibility semantics;
- long text/code/table/URL layout constraints;
- resize/DPI assumptions;
- service-worker cache invalidation/offline shell;
- dangerous HTML/Markdown rendering/XSS;
- secret exposure;
- active profile/model/status display;
- hidden legacy labels.

Не объявляй визуальный PASS без PHASE B screenshots and interaction.

### 12.8. Security, privacy и dependencies

Построй threat model и проверь:

- command/PowerShell/shell injection;
- arbitrary file read/write;
- path/junction/symlink confusion;
- unsafe deserialization;
- secret leakage;
- weak local auth/CORS/bind defaults;
- privilege boundary errors;
- tool/approval bypass;
- prompt injection;
- SSRF;
- dependency/image pinning;
- known-vulnerability scan только с честной датой базы;
- typosquatting/untrusted install hooks;
- credentials in history/config/tests;
- log injection;
- unsafe retry of destructive actions.

Не выдавай offline/stale vulnerability database за актуальную гарантию.

### 12.9. Performance и resource risks по коду

Найди:

- unbounded queues/history/logs/cache;
- O(n²) и repeated full scans;
- blocking I/O in async paths;
- runaway retries/timers;
- leaked tasks/processes/files/sockets;
- large copies/serialization;
- missing pagination/backpressure;
- expensive startup work;
- GPU/model lifecycle assumptions;
- frontend render storms;
- storage growth without retention.

Все измерения оставь PHASE B.

---

## 13. PHASE A6 — поиск неизвестных классов ошибок

Где безопасно и технически разумно, выполни:

- property-based testing чистых функций и parsers;
- grammar/schema-aware fuzzing API payloads, configs, tool calls и event parsers;
- stateful/model-based tests для детерминированных state machines;
- metamorphic tests для эквивалентных inputs/config order;
- mutation testing критических validation/permission paths;
- differential tests между двумя реализациями/fallbacks;
- seeded random action sequences на fake runtime;
- static call graph/data flow/taint-like tracing;
- TODO/FIXME/HACK/dead/unreachable/legacy branch review;
- exception-path inventory;
- negative-space review: функции, о которых документация обещает больше, чем код.

Установи budgets/watchdogs. Не допускай бесконтрольного fuzz, расхода диска или зависания Work.

---

## 14. PHASE A7 — матрица сценариев и точный план живого аудита

Создай `STATIC_SCENARIO_MATRIX.csv` минимум со столбцами:

```text
scenario_id,feature_ids,requirement_ids,domain,component,method,
preconditions,state_before,stimulus,oracle,invariants,profile,
network_state,resource_state,storage_state,concurrency,permissions,
locale_encoding,phase,status,evidence_ids,finding_ids,notes
```

Для runtime-части создай `LIVE_SCENARIO_QUEUE.csv`:

```text
order,scenario_id,priority,risk,domain,feature_ids,requirement_ids,
exact_preconditions,exact_commands_or_actions,required_profile,
required_services,state_setup,stimulus,expected_oracle,
telemetry_to_capture,safety_isolation,cleanup,repeat_count,
time_budget,source_findings,dependencies,status,notes
```

Каждый live-сценарий должен быть достаточно точным, чтобы PHASE B не придумывала постановку заново.

Минимальные измерения для PHASE B:

- все фактические profiles;
- stopped/starting/ready/busy/degraded/recovering;
- online/offline/intermittent/DNS/registry-blocked;
- model ready/loading/slow/error/OOM/disconnected;
- API/WS normal/slow/disconnected/reconnected/duplicate/out-of-order;
- empty/tiny/large/Unicode/Cyrillic/emoji/code/JSON/malformed inputs;
- tool success/failure/timeout/cancel/partial/huge/permission/retry;
- one/multi-session and concurrent/cancel+new request;
- storage normal/locked/read-only/nearly-full/corrupt-copy/partial-write;
- ordinary/spaces/Cyrillic/long paths;
- normal/elevated/denied permissions where applicable;
- clock/timeout boundaries;
- GUI sizes/DPI/stream/error/long-history states.

Используй pairwise для широкого покрытия, 3-way/4-way для high-risk пересечений, boundary values, decision tables, model-based sequences и targeted exhaustive для малых конечных пространств.

---

## 15. Формат finding PHASE A

Каждый `findings/JARVIS-NNNN.md`:

```yaml
id: JARVIS-0001
title: "..."
kind: defect | security | reliability | performance | ux | spec-gap | test-gap
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

Обязательные разделы:

1. Summary.
2. Contract and impact.
3. Static evidence with exact paths/symbols/line ranges.
4. Hermetic reproduction, если есть.
5. Expected vs observed.
6. Why runtime confirmation is or is not required.
7. Root cause: confirmed / strong hypothesis / unknown.
8. Affected paths and data flow.
9. Security/data-integrity implications.
10. Exact PHASE B confirmation/refutation procedure.
11. Suggested remediation direction без production fix.
12. Regression risks.
13. Acceptance criteria draft.
14. Related findings/spec/test gaps.

Severity не завышай. Дедуплицируй общую root cause, но сохрани все симптомы/scenarios.

---

## 16. Candidate tasks для Spark — только заготовки

Для статически подтверждённых или сильных findings можно создать `candidate_tasks/CTASK-NNNN.md`, но:

- status должен быть `AWAITING_PHASE_B`, `BLOCKED_BY_SPEC` либо `STATIC_ONLY_REVIEW_REQUIRED`;
- не помещай их в финальную Spark queue;
- не создавай `spark/READY`;
- укажи, какие runtime checks должны быть выполнены перед переводом в READY;
- декомпозируй до одного дефекта/одной root cause/не более одной подсистемы;
- перечисли context files, tentative allowed files, regression test и acceptance criteria;
- не заставляй Spark принимать архитектурное решение без контракта.

PHASE B будет обязана подтвердить, исправить и переиздать эти задачи в финальном `spark/tasks/`.

---

## 17. Handoff для PHASE B

Создай `handoff/PHASE_B_START_HERE.md`. Он должен содержать:

1. run path и source commit;
2. что именно PHASE A выполнила;
3. какие команды и suites реально запускались;
4. статистику `PASS_HERMETIC/FAIL_HERMETIC/BLOCKED/INCONCLUSIVE`;
5. список static-confirmed findings;
6. список probable-runtime findings;
7. top SPEC/TEST gaps;
8. точный порядок `LIVE_SCENARIO_QUEUE.csv`;
9. machine prerequisites для каждого блока;
10. destructive-risk/isolation notes;
11. source drift policy;
12. какие artifacts PHASE B должна обновить, а какие сохранить immutable;
13. условия, при которых объединённый аудит считается завершённым;
14. указание не создавать Spark READY до конца runtime-кампании.

### Source drift policy

PHASE B должна сравнить текущий production tree с `source_commit`, исключив только:

```text
.audit/**
docs/audit/**
```

Если production-код изменился:

- сохранить `SOURCE_DRIFT.md`;
- перечислить changed files/commits;
- повторно статически проверить затронутые features/contracts;
- не переносить старый вывод на новый код без переоценки;
- продолжить тот же run только при управляемом drift, иначе создать derived run с явной связью.

Опиши эту политику в отдельном `SOURCE_DRIFT_POLICY.md`.

---

## 18. Traceability и consistency checks

Поддерживай связь:

```text
feature -> requirement/invariant -> scenario/test -> result -> evidence
        -> finding -> live confirmation scenario -> candidate task
```

Создай consistency-check harness, который обнаруживает:

- неизвестные IDs;
- duplicate IDs;
- feature без contract/spec-gap;
- high-risk requirement без positive/negative scenario;
- FAIL без finding;
- finding без evidence;
- probable-runtime finding без PHASE B scenario;
- candidate task без source finding;
- runtime scenario, ошибочно отмеченный PASS на PHASE A;
- broken paths/references.

Запусти его перед завершением.

---

## 19. Критерии завершения PHASE A

Не ставь `phase_a.status = COMPLETE`, пока:

1. проиндексированы все tracked source/config/test/script/doc files;
2. построена фактическая карта компонентов, entry points, data stores и trust boundaries;
3. каждая публичная feature получила ID;
4. каждой feature назначен contract/invariant или SPEC-GAP;
5. существующие tests инвентаризированы и все безопасные hermetic checks выполнены либо точно заблокированы;
6. создано статическое coverage/gap описание;
7. выполнен глубокий аудит всех подсистем;
8. применены методы поиска неизвестных ошибок там, где это разумно;
9. findings имеют evidence и честный статус;
10. каждый probable-runtime finding имеет точный live scenario;
11. создана полная `LIVE_SCENARIO_QUEUE.csv` с safety/cleanup/oracles;
12. создан handoff для PHASE B;
13. consistency-check проходит;
14. production-код не был исправлен или случайно изменён;
15. не созданы маркеры завершённого объединённого аудита или Spark READY.

Если не всё возможно, установи `COMPLETE_WITH_BLOCKERS` только когда вся доступная PHASE A работа закончена, а блокеры документированы. `INCOMPLETE` используй, если работа реально оборвалась.

---

## 20. Финальный ответ Work

Не выгружай весь аудит в чат. Сначала сохрани артефакты. Затем сообщи:

- путь из `.audit/LATEST_STATIC_RUN.txt`;
- `source_commit` и branch;
- количество features/requirements/scenarios/tests;
- результаты hermetic checks;
- findings по severity и статусу;
- крупнейшие SPEC/TEST gaps;
- количество и приоритет live-сценариев;
- точный путь к `handoff/PHASE_B_START_HERE.md`;
- commit/branch, где сохранены audit artifacts, либо точный blocker публикации;
- явную фразу, что runtime ещё не проверен и Spark пока заблокирован.

Красивый отчёт без полного file inventory, выполненных hermetic checks, traceability и исполнимого handoff считается провалом PHASE A.
