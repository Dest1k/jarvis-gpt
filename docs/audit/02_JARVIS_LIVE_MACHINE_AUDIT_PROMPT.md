# JARVIS — PHASE B: ПОЛНЫЙ АУДИТ НА ЖИВОЙ ЦЕЛЕВОЙ МАШИНЕ

Ты работаешь как главный инженер по качеству, SRE, системный архитектор, adversarial tester, специалист по безопасности и координатор независимых субагентов. Это **вторая половина двухкомпонентного доказательного аудита JARVIS**.

PHASE A уже должна была разобрать репозиторий без доступа к целевой машине, создать карту системы, контракты, findings и точную очередь живых сценариев. Твоя задача — продолжить **тот же audit run** на реальной Windows-машине, проверить всё, что нельзя доказать чтением кода, подтвердить или опровергнуть выводы PHASE A, дополнить пропущенные сценарии, а затем создать окончательную атомарную очередь исправлений для Codex Spark.

Это не просьба просто запустить `doctor.ps1`, посмотреть несколько логов и написать впечатление. Требуется систематическая испытательная кампания с наблюдаемыми oracles, воспроизводимыми evidence, fault injection в безопасных изолированных контурах, проверкой GUI, реальной LLM, Docker/WSL/GPU, persistence, offline-first, конкурентности, безопасности, ресурсов и восстановления.

---

## 0. Фиксированные каталоги — не перепутай

```text
D:\jarvis-gpt   — единственный рабочий Git-репозиторий и исходный код
D:\jarvis       — модели, Docker/runtime-данные, кеши, пользовательские данные,
                  большие логи и тяжёлые evidence-файлы
```

Начни строго так:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
```

Ожидаемый корень — `D:/jarvis-gpt` с допустимой разницей в регистре и разделителях. `D:\jarvis` никогда не считай репозиторием и не создавай там `.audit`.

Малые текстовые audit-артефакты хранятся в:

```text
D:\jarvis-gpt\.audit\...
```

Тяжёлые screenshots, видео, дампы, трассы, профилировочные данные и длинные логи:

```text
D:\jarvis\audit-evidence\<RUN_ID>\...
```

Для каждого внешнего evidence-файла сохраняй путь, SHA-256, размер, тип, timestamp, sanitization и связанные scenario/finding IDs.

---

## 1. Обязательный handoff из PHASE A

До любых runtime-действий проверь наличие:

```text
.audit/LATEST_STATIC_RUN.txt
```

Файл должен содержать относительный путь вида:

```text
.audit/runs/<RUN_ID>
```

В найденном каталоге обязаны существовать как минимум:

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

Если handoff отсутствует, run не читается, `phase_a.status` равен `IN_PROGRESS`/`INCOMPLETE` или нет исполнимой live-очереди, **не изображай продолжение цельного аудита**. Создай точный blocker и остановись. Не создавай новый независимый аудит молча и не запускай Spark по частичным материалам.

Если PHASE A имеет `COMPLETE_WITH_BLOCKERS`, прочитай блокеры и продолжай всё, что доступно; каждый унаследованный пробел должен получить конечный статус в объединённом отчёте.

Сначала полностью прочитай:

1. `handoff/PHASE_B_START_HERE.md`;
2. `PIPELINE_STATE.json`;
3. `SOURCE_BASELINE.json`;
4. `SOURCE_DRIFT_POLICY.md`;
5. `BEHAVIORAL_CONTRACT.md`;
6. `STATIC_FINDINGS_INDEX.md` и связанные findings;
7. `LIVE_SCENARIO_QUEUE.csv`;
8. `SPEC_GAPS.md`, `TEST_GAPS.md`, `ARCHITECTURE_RISKS.md`;
9. актуальные repository instructions (`AGENTS.md`, `CLAUDE.md`, `CODEX.md` и эквиваленты), если они существуют.

Не полагайся на скрытый контекст PHASE A: единственный источник её работы — сохранённые файлы.

---

## 2. Проверка source drift

Зафиксируй текущий production commit и сравни его с `source_commit` из `SOURCE_BASELINE.json`, исключив только audit/prompt-артефакты:

```text
.audit/**
docs/audit/**
```

Если production-код не изменился, продолжай тот же `RUN_ID`.

Если изменился:

1. создай `SOURCE_DRIFT.md`;
2. перечисли commits, changed files и затронутые FEAT/REQ/INV/SCN IDs;
3. перечитай изменённые production-файлы;
4. повтори релевантные static/hermetic checks;
5. переоцени связанные findings и live scenarios;
6. не переноси PASS или root cause на новый код автоматически.

При небольшом управляемом drift продолжай тот же run с полным журналом. При крупном drift, который делает карту/контракты PHASE A недостоверными, создай derived run с новым ID и явной ссылкой `derived_from`, перенеси только проверенные артефакты и заново построй затронутые разделы.

Никогда не сбрасывай пользовательские изменения и не присваивай себе грязный working tree.

---

## 3. Scope и запрет на установочный аудит

Первичная установка исключена. Считай, что изделие уже подготовлено:

- исходники находятся в `D:\jarvis-gpt`;
- модели, образы и runtime-данные находятся под `D:\jarvis` либо в фактически настроенных локальных путях;
- базовый запуск в принципе возможен.

Не трать кампанию на чистую машину, первоначальное скачивание моделей, bootstrap с нуля и выбор installer.

В scope входят все свойства **готового изделия**:

- холодный/тёплый старт из подготовленного состояния;
- stop/restart/recovery;
- частично запущенный и деградированный стек;
- правильное разрешение всех фактических профилей и моделей;
- offline-first после подготовки;
- backend, API, streaming, WebSocket и GUI;
- реальная локальная LLM, provider и tool-loop;
- missions, memory, persona, learning, autonomy и approvals;
- browser/web/document/filesystem/Docker/host tools;
- persistence, corruption handling и восстановление;
- concurrency, cancel, retry, idempotency;
- безопасность, privacy и trust boundaries;
- performance, resource use, repeated cycles и soak.

Фактический список профилей, функций и требований бери из текущего репозитория и PHASE A. Не навязывай устаревшее количество профилей. Любой конфликт README/config/UI/runtime оформляй как SPEC-GAP или defect, а не разрешай догадкой.

---

## 4. Правила безопасной работы на живой машине

1. Не уничтожай пользовательские данные, модели, Docker volumes, WSL distributions, секреты или незакоммиченные изменения.
2. Запрещены `git reset --hard`, агрессивный `git clean`, broad Docker prune, удаление рабочих volumes, factory reset Docker/WSL и массовое удаление runtime-каталогов.
3. Не исправляй production-код в ходе аудита. Разрешены только `.audit/**`, внешние evidence и изолированные harness/repro/fixtures. Объект исследования должен оставаться стабильным.
4. Любой destructive/corrupt/disk-full/permission/chaos тест выполняй только на копии БД, синтетических данных, отдельном temp root, отдельном Compose project name или тестовом volume.
5. До остановки Docker, WSL, host bridge, backend или общих портов проверь, не затронет ли это посторонние workloads. Если изоляция не доказана — используй simulation/targeted process и пометь реальный вариант `BLOCKED_BY_SAFETY`.
6. Не атакуй внешние сайты и сервисы. Security tests выполняй против локального JARVIS, loopback test servers и принадлежащих проекту синтетических ресурсов.
7. Не исполняй потенциально опасные команды, сгенерированные LLM, на реальной системе. Используй mock/sandbox/dry-run/allowlist и синтетические цели.
8. Не записывай реальные токены, секреты, приватные сообщения или PII. Маскируй значения и сохраняй лишь диагностически полезные фрагменты.
9. Не устанавливай и не обновляй runtime-зависимости продукта без крайней необходимости. Временные audit tools разрешены только изолированно, с фиксацией версии и последствий.
10. Не push и не открывай PR без прямого указания пользователя. Локальные commits с audit artifacts допустимы только если working tree позволяет безопасно отделить их от пользовательских изменений.
11. Если критический дефект блокирует кампанию, сначала создай finding и evidence. Временный workaround допустим лишь в изолированном audit-контуре; все результаты с workaround должны быть помечены.
12. Не объявляй PASS по чтению кода. Runtime PASS требует реально выполненного сценария и проверенного oracle.

---

## 5. Статусы и доказательная дисциплина

Для live-сценариев используй:

- `PASS` — реально выполнен, oracle проверен, evidence сохранён;
- `FAIL` — наблюдаемое поведение нарушает контракт/invariant;
- `BLOCKED_BY_ENV` — отсутствует конкретный ресурс/инструмент;
- `BLOCKED_BY_SAFETY` — реальный тест небезопасен без изоляции;
- `BLOCKED_BY_SPEC` — ожидаемое поведение неоднозначно;
- `NOT_APPLICABLE` — доказуемо неприменим;
- `INCONCLUSIVE` — данные противоречивы или flaky;
- `NOT_RUN` — допустим только пока PHASE B не завершена.

Каждый подтверждённый дефект воспроизведи минимум дважды из документированного исходного состояния, если повтор безопасен и разумен. Для flaky-поведения фиксируй `runs/failures`, seed, интервалы и условия.

Для каждой команды/действия сохраняй:

- UTC timestamp;
- exact command или точную GUI-последовательность;
- working directory;
- профиль и релевантные env values с redaction;
- state before/after;
- exit code;
- duration;
- stdout/stderr/log/screenshot paths;
- process/container/resource deltas;
- SCN/TEST/EVID/finding IDs;
- cleanup result.

Не смешивай:

- подтверждённый defect;
- refuted static hypothesis;
- operational limitation среды;
- SPEC-GAP;
- TEST-GAP;
- performance observation без SLO;
- recommendation.

---

## 6. Продолжение единого run-каталога

Продолжай каталог из `.audit/LATEST_STATIC_RUN.txt`. Не создавай второй несвязанный набор отчётов.

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
  SECURITY_REPORT.md
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
      SPARK-0001.md
  machine/
    scenario_results.jsonl
    findings.jsonl
    spark_tasks.jsonl
```

PHASE A artifacts не перезаписывай так, чтобы исчезла история. Исправления factual errors в них делай через отдельные addendum или журнал с причиной.

Только после выполнения критериев завершения создай:

```text
.audit/LATEST_COMPLETE_RUN.txt
.audit/LATEST_RUN.txt
```

Оба файла должны содержать единственную относительную строку:

```text
.audit/runs/<RUN_ID>
```

И только тогда создай пустой marker:

```text
.audit/runs/<RUN_ID>/spark/READY
```

Если PHASE B оборвалась или имеет непроработанные `NOT_RUN`, marker `READY` запрещён.

---

## 7. Субагенты и сериализация живой среды

Если доступны Ultra/subagents, раздели работу на read-only и независимые направления:

1. runtime/launcher/profiles/Docker/WSL/offline;
2. backend/API/stream/WebSocket;
3. real LLM/agent/tools/missions/memory;
4. frontend/GUI/accessibility/service worker;
5. persistence/concurrency/fault/recovery;
6. security/privacy;
7. performance/resources/soak;
8. независимый adversarial reviewer.

Но любые действия, которые меняют один и тот же runtime, Docker project, GPU, model server, persistent store или GUI state, выполняй **строго последовательно**. Субагенты не должны одновременно перезапускать стек, переключать профиль, менять env, занимать GPU или портить общую БД.

Главный агент обязан:

- назначать ownership сценарию;
- проверять первичный evidence;
- дедуплицировать findings по root cause;
- не принимать краткий отчёт без точных команд/paths/oracles;
- обновлять `AUDIT_STATE.md` после каждого крупного блока;
- после compaction перечитывать `PIPELINE_STATE.json`, `AUDIT_STATE.md` и очередь;
- сохранять возможность безопасного продолжения с последней точной команды.

---

## 8. PHASE B0 — preflight и живой baseline

1. Проверь Git root, branch, status, source drift и handoff.
2. Зафиксируй Windows build, PowerShell, Python, Node/npm, Docker Desktop/Engine/Compose, WSL distributions/version, GPU, driver, CUDA, RAM, VRAM, CPU, диски и свободное место.
3. Зафиксируй фактические runtime/data/model/cache/log/state roots; не выводи секреты.
4. Сними processes, services, listening ports, containers, networks, volumes и baseline resources.
5. Определи штатные команды start/stop/restart/status/doctor/dispatcher для текущего commit.
6. Проверь наличие и целостность требуемых локальных моделей/образов без скачивания.
7. Зафиксируй initial state: stopped/partial/running и не меняй его до записи baseline.
8. Выполни безопасный baseline smoke готового изделия.
9. Сохрани фактические health/readiness signals и их согласованность.
10. Проверь full repository test/static suite на целевой ОС, если это не меняет production state; сопоставь с PHASE A.

`LIVE_BASELINE.md` должен позволять другому инженеру воспроизвести среду и отличить defect от внешней конфигурации.

---

## 9. PHASE B1 — исполнение live-очереди и расширение покрытия

Исполняй `LIVE_SCENARIO_QUEUE.csv` в порядке зависимостей и риска. Для каждой строки:

1. проверь preconditions;
2. создай безопасную изоляцию;
3. запиши state before;
4. выполни exact stimulus;
5. проверь каждый oracle/invariant;
6. собери telemetry/evidence;
7. выполни cleanup;
8. проверь возврат к known-good state;
9. обнови status и связанные findings.

Не ограничивайся очередью, если живая система открыла новые компоненты, состояния или failure modes. Добавь новые `SCN-...`, сохраняя traceability.

Для широких комбинаций используй pairwise; для high-risk пересечений — 3-way/4-way; для малых конечных state machines — targeted exhaustive; для последовательностей — model-based/random seeded exploration; для boundary/error inputs — equivalence classes и boundary values.

---

## 10. PHASE B2 — profiles, model proof, Docker/WSL и offline-first

Для **каждого фактически поддерживаемого активного профиля** выполни отдельный независимый проход.

Докажи цепочку:

```text
launcher/CLI selection
  -> env/config precedence
  -> resolved Compose/service command
  -> container/process runtime
  -> provider metadata/health
  -> реально загруженная model identity/config
  -> UI/API-reported active profile/model
```

Не принимай переменную окружения за доказательство загрузки модели.

Проверь:

- cold и warm start;
- start из другого cwd;
- paths с пробелами/кириллицей;
- repeat start при уже запущенном стеке;
- stop/restart всей системы и отдельных components;
- partial stack;
- stale PID/lock/container/network;
- occupied ports;
- unknown/misspelled profile;
- empty/malformed/conflicting env values;
- precedence общих/profile env sources;
- model/profile mismatch и stale dispatcher;
- startup ordering/readiness races;
- health false-positive/false-negative;
- service crash и restart policy;
- Docker/WSL interruption и recovery, только безопасно;
- process/container/network/port/GPU cleanup;
- repeated cycles и накопление мусора;
- actionable diagnostics без внутренних загадок.

### Строгая offline-проверка

После подтверждения, что необходимые локальные assets уже существуют:

1. зафиксируй baseline network activity;
2. заблокируй интернет/DNS/registry для тестового процесса или изолированного контура, сохранив loopback/local LAN, необходимые самому стеку;
3. запускай штатным способом без `pull`, download, package resolution и remote metadata;
4. наблюдай DNS, HTTP(S), registry и package-manager attempts;
5. докажи, что локальное ядро стартует и работает;
6. отдельно проверь сетевые функции: они должны деградировать понятно, ограниченно по времени и не ломать локальные функции;
7. восстанови сеть и проверь recovery.

Создай `OFFLINE_PROOF.md`: метод изоляции, captures, unexpected calls, PASS/FAIL по каждому профилю и ограничения доказательства.

---

## 11. PHASE B3 — backend, API, streaming, WebSocket и GUI

### API/stream/WebSocket

Для каждого endpoint/event из FEATURE_CATALOG проверь:

- normal request/response;
- missing/extra/wrong/oversized/malformed payload;
- stable error semantics и отсутствие silent success;
- auth/origin/CORS/bind assumptions;
- timeout/cancel/retry/idempotency;
- slow consumer/backpressure;
- reconnect, duplicate connection, half-open и abrupt close;
- server restart во время stream;
- missing/duplicate/late/out-of-order terminal events;
- cross-session isolation;
- correlation/logging/redaction;
- schema compatibility backend ↔ frontend.

### GUI и реальное взаимодействие

Проверяй функционально, а не только по screenshot:

- empty/first-run-ready state готового изделия;
- chat submit/stream/cancel/retry;
- tool progress и approvals;
- backend/model/tool/network errors;
- offline/reconnect;
- conversation/history restore;
- missions, memory, files, diagnostics, status и прочие фактические панели;
- active profile/model/status truth;
- no stale optimistic success;
- duplicate submit/event prevention;
- resize/maximize/restore/scroll/focus во время stream;
- минимальный разумный размер, 1280×720, 1920×1080 и доступные DPI 100/125/150/200%;
- длинные сообщения, code blocks, tables, URL/слова, кириллица, emoji, mixed content;
- keyboard navigation, Enter/Shift+Enter, cancel, copy/paste;
- accessibility basics: focus visibility, labels, semantic roles, contrast where measurable;
- service-worker cache/offline shell/update invalidation;
- отсутствие XSS/unsafe HTML rendering;
- отсутствие user-facing inactive/legacy model labels без контракта;
- recovery actions понятны без чтения traceback.

Каждый visual finding: steps, state, viewport, DPI, screenshot, expected/observed, reproducibility.

---

## 12. PHASE B4 — real LLM, agent core, tools, missions и memory

Разделяй:

1. deterministic orchestration tests с fake/mock provider;
2. real-model behavioral smoke/robustness tests.

Не используй точный nondeterministic текст как единственный oracle. Проверяй schemas, side effects, permissions, terminal states, citations/provenance и bounded semantic rubrics. LLM-as-judge — только дополнительный сигнал.

Проверь:

- пустые/короткие/длинные/context-near-limit запросы;
- Unicode, кириллица, emoji, Markdown, code, JSON/XML, shell output, длинные строки;
- timeout, slow tokens, disconnect, empty response, malformed/duplicate chunks;
- invalid tool names/args/types/JSON;
- multiple/dependent tool calls и partial success;
- timeout/cancel/retry/idempotency around tools;
- cancel до, во время и после side effect;
- huge/binary-like/escape-sequence outputs;
- planner/executor disagreement;
- loop/step budgets и runaway retries;
- provider unavailable/degraded/recovered;
- honest failure вместо hallucinated success;
- user/file/web/tool/memory prompt injection;
- secret exfiltration attempts на синтетических secrets;
- filesystem/root/path boundary;
- approval exact binding and one-shot semantics;
- cross-session leakage;
- mission create/run/resume/block/complete/final report;
- restart с незавершённой mission/task;
- memory/persona/learning write/read/dedup/delete/isolation/poisoning;
- autonomy/self-heal safety and no silent dangerous mutation;
- отображаемые agent steps соответствуют реальным actions.

Для каждого зарегистрированного tool выполни минимальную матрицу:

```text
success, invalid input, denied, timeout, cancel, partial, huge result,
retry, concurrent use, unsafe request, encoding/path edge case
```

### Web/browser/document/file surfaces

Используй локальные loopback fixtures и синтетические документы для adversarial cases:

- SSRF/private/loopback rules и redirect chains;
- malformed URLs/schemes/credentials/DNS changes;
- prompt injection in pages/documents;
- provenance/citation correctness;
- cache freshness/stale labeling;
- browser approval/session/tab isolation;
- timeout/cancel/recovery;
- malicious filenames, traversal, junction/symlink, archive-slip;
- MIME/extension mismatch;
- oversized/compressed inputs в безопасных лимитах;
- Word/Excel/PDF/PPTX/text extraction errors;
- formulas/external links/macros as data, not execution;
- copy-on-write, original preservation и output collisions;
- Unicode/encoding/OCR fallback;
- chunk/index/recall consistency;
- failed parser does not corrupt global state.

---

## 13. PHASE B5 — concurrency, fault injection, recovery и data integrity

Безопасно проверь:

- several requests in one and multiple sessions;
- parallel tool calls;
- cancel racing terminal event;
- new request immediately after cancel;
- backend restart during stream;
- frontend/WebSocket/provider/tool-worker interruption;
- delayed/duplicate/out-of-order events;
- lock contention;
- process kill during write в изолированной копии;
- read-only state directory;
- nearly-full test volume;
- temporary persistent-store unavailability;
- partial/corrupt config/cache/index/session/DB copy;
- clock/timeout boundaries;
- rapid repeated start/stop/restart;
- orphan tasks/processes/containers/ports/GPU allocations;
- recovery to known-good state without hidden corruption.

Для SQLite/WAL и иных persistent stores проверяй transaction atomicity, schema/migrations, idempotency, duplicate events, lock retries, integrity checks, backup/restore semantics и user-facing diagnostics.

После каждого fault:

1. проверь видимый результат;
2. проверь persisted state;
3. проверь logs/audit events;
4. восстанови систему;
5. повтори normal smoke;
6. сравни с baseline hashes/counts/invariants.

---

## 14. PHASE B6 — security и privacy

Используя threat model PHASE A, выполни безопасные локальные проверки:

- command/shell/PowerShell injection;
- path traversal/arbitrary read-write/junction-symlink confusion;
- prompt injection from user/file/web/tool/memory;
- approval/tool permission bypass;
- unsafe deserialization/schema bypass;
- API/WS exposure, bind, CORS, auth/session isolation;
- SSRF against controlled local targets;
- secret leakage in logs/UI/exceptions/env dump/model context/audit;
- dangerous broad filesystem/Docker/host permissions;
- malicious filenames/archive paths/MIME confusion;
- log injection/terminal escape sequences;
- retry of irreversible actions;
- action represented as user-approved when it was not;
- dependency/image pinning and reproducibility;
- current dependency scans only with recorded database freshness.

Используй synthetic secrets. Не публикуй exploit material против внешних целей. Critical/high finding должен иметь минимальный безопасный PoC, exact boundary violated и containment/remediation criteria.

---

## 15. PHASE B7 — performance, ресурсы и soak

Отдельно для каждого поддерживаемого профиля измерь повторно:

- cold/warm startup и time-to-ready;
- time-to-first-token и completion latency типовых запросов;
- API/UI latency без LLM и с LLM;
- throughput/queue/backpressure при ограниченной параллельности;
- CPU/RAM/VRAM/disk I/O/network;
- resource release after completion/cancel/shutdown;
- repeated start/stop and request cycles;
- log/cache/history/index/temp growth;
- long conversation/mission;
- bounded soak;
- recovery after resource pressure.

Сохраняй warmup отдельно от steady state, минимум несколько измерений, percentiles/dispersion where meaningful и точные условия. Не называй performance defect без contract/SLO или явной практической непригодности; при отсутствии SLO создай baseline/recommendation.

Soak должен иметь watchdog, resource/disk ceilings и безопасный abort. После него проверь functional smoke и leaks/orphans.

---

## 16. PHASE B8 — подтверждение и опровержение PHASE A

Для каждого finding PHASE A:

- `static-confirmed`: подтвердить runtime там, где это добавляет уверенность; если runtime не нужен, проверить контракт и сохранить статус;
- `probable-runtime`: выполнить связанный scenario и перевести в confirmed/refuted/inconclusive;
- `spec-gap`: попытаться разрешить по наблюдаемому public behavior и актуальным docs, но не выдумывать product decision;
- `test-gap`: создать точную будущую regression/test-foundation задачу;
- `inconclusive`: собрать недостающее evidence либо оставить честный blocker.

Не удаляй опровергнутый finding. Сохрани его историю со статусом `refuted`, доказательством и причиной расхождения.

Новые runtime findings нумеруй продолжая существующую последовательность. Дедуплицируй общую root cause, но сохраняй симптомы и scenarios.

---

## 17. Формат финального finding

Каждый `findings/JARVIS-NNNN.md` должен иметь front matter:

```yaml
id: JARVIS-0001
title: "..."
kind: defect | security | reliability | performance | ux | spec-gap | test-gap
severity: critical | high | medium | low
priority: P0 | P1 | P2 | P3
status: confirmed | refuted | blocked-by-spec | blocked-by-env | inconclusive | accepted-risk
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

Обязательные разделы:

1. Summary.
2. Contract/invariant and impact.
3. Preconditions.
4. Exact reproduction.
5. Expected vs observed.
6. Evidence paths/excerpts.
7. Reproduction count/flakiness.
8. Root cause: confirmed/hypothesis/unknown.
9. Code/data/control flow map.
10. Security/data-integrity implications.
11. Workaround, если безопасен.
12. Remediation direction.
13. Regression risks.
14. Binary acceptance criteria.
15. Related findings/spec/test gaps.
16. Spark task mapping or reason `NO_CODE_CHANGE`.

Severity:

- `critical`: arbitrary execution, secret exposure, irreversible data loss, major trust-boundary compromise;
- `high`: primary flow broken, wrong active model/profile, substantial data/recovery/security failure;
- `medium`: material functional/recovery/performance/UX defect without immediate catastrophe;
- `low`: narrow edge case, diagnostics, cosmetic or minor documentation issue.

Не завышай severity ради эффекта.

---

## 18. Создание окончательной очереди для Spark

После завершения объединённого аудита преобразуй каждый confirmed code/config/test defect в атомарную задачу. Spark не должен повторять исследование или угадывать архитектуру.

### Декомпозиция

- одна задача = один defect/root cause/обязательный подготовительный шаг;
- предпочтительно одна подсистема и не более пяти production-файлов;
- крупную проблему разложи на test/contract foundation, маленькие implementation steps и validation checkpoint;
- неоднозначный product decision получает `BLOCKED_BY_SPEC`;
- задачи не должны требовать скрытого контекста PHASE A/B;
- укажи dependencies, file conflicts, safe order и batches;
- regression/observability foundations идут раньше fixes;
- после каждого batch создавай validation task;
- не запускай параллельно задачи, меняющие общий runtime/state/GPU или те же файлы.

### Формат `spark/tasks/SPARK-NNNN.md`

```yaml
id: SPARK-0001
title: "..."
source_findings: []
type: investigation | test | implementation | refactor | documentation | validation
status: READY | BLOCKED_BY_SPEC | BLOCKED_BY_ENV | DONE
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
```

Каждая task обязана содержать:

1. Goal.
2. Why it matters.
3. Known-good baseline/run/commit/profile.
4. Exact FAIL reproduction before change.
5. Evidence paths и короткие excerpts.
6. Confirmed root cause либо явно маркированную hypothesis.
7. Relevant code map.
8. Allowed/forbidden scope.
9. Minimal fix strategy.
10. Test-first requirement.
11. Exact narrow + neighboring + full validation commands.
12. Binary acceptance criteria.
13. Regression checklist и второй профиль/соседние surfaces.
14. Cleanup/restore procedure.
15. Stop/escalation conditions.
16. Expected task report.
17. Suggested local commit message.

В каждую задачу вставь:

> Не объявляй задачу выполненной по чтению кода. Сначала воспроизведи исходный FAIL, затем добавь regression test, сделай минимальный patch, выполни все validation-команды, проверь соседние сценарии и `git diff`.

Создай `SPARK_QUEUE.csv`:

```text
order,task_id,priority,status,batch,depends_on,conflicts_with,risk,
scope,profiles,components,expected_tests,notes
```

Создай `SPARK_PROGRESS.md` с пустой историей выполнения и source audit metadata.

Создай `SPARK_MASTER_PROMPT.md`, совпадающий по правилам с `docs/audit/03_JARVIS_SPARK_REMEDIATION_PROMPT.md`, но дополненный фактическим RUN_ID и queue paths.

`START_HERE_FOR_SPARK.md` должен дать человеку один короткий copy-paste prompt и явно проверить marker `spark/READY`.

---

## 19. Traceability и consistency checks

Финальная цепочка:

```text
feature -> requirement/invariant -> static/live scenario -> method/result
        -> evidence -> finding -> Spark task -> validation checkpoint
```

Обнови `TRACEABILITY.csv` и machine-readable JSONL.

Запусти consistency harness, который обнаруживает:

- unknown/duplicate IDs;
- feature без contract/spec-gap;
- high-risk requirement без positive/negative/recovery scenario;
- live `NOT_RUN` в завершённом аудите;
- FAIL без finding;
- confirmed finding без evidence;
- confirmed code defect без Spark task или `NO_CODE_CHANGE` rationale;
- Spark task без finding/acceptance/tests;
- broken paths;
- duplicate queue order;
- cyclic dependencies;
- READY task с невыполненной dependency;
- marker READY при незавершённой PHASE B.

Consistency harness должен PASS перед созданием `spark/READY`.

---

## 20. Критерии завершения объединённого аудита

Не ставь `phase_b.status = COMPLETE`, не создавай `LATEST_COMPLETE_RUN.txt` и `spark/READY`, пока одновременно не выполнено:

1. PHASE A handoff проверен и source drift обработан.
2. Снята полная живая environment/baseline картина.
3. Все фактические активные профили реально запущены либо имеют точный конечный blocker.
4. Для доступных профилей доказано реальное profile → model mapping.
5. Offline-first проверен наблюдением сетевой активности, а не только config review.
6. Выполнены все безопасно доступные штатные tests/static checks на целевой ОС.
7. Каждая публичная feature имеет happy/error/recovery coverage либо конечный documented gap/blocker.
8. API/stream/WebSocket/GUI проверены функционально в основных и отказных состояниях.
9. Real LLM/agent/tool/memory/mission surfaces имеют deterministic и real-model coverage.
10. High-risk concurrency/cancel/retry/crash/recovery/data-integrity transitions выполнены либо конкретно заблокированы.
11. Security/privacy tests связаны с threat model.
12. Performance baseline, repeated cycles, resource release и bounded soak зафиксированы.
13. Все `probable-runtime` findings PHASE A подтверждены, опровергнуты или имеют конечный blocker.
14. Все строки live scenario matrix имеют конечный статус; `NOT_RUN` отсутствует.
15. Findings дедуплицированы, имеют evidence/reproduction/root cause/confidence.
16. Все confirmed code/config/test defects преобразованы в атомарные Spark tasks либо имеют обоснование `NO_CODE_CHANGE`.
17. Queue имеет dependencies/conflicts/batches/validation checkpoints.
18. Traceability consistency check PASS.
19. `RESIDUAL_RISK.md` честно перечисляет недоказанное.
20. Production-код не был молча исправлен, пользовательские данные не повреждены, cleanup проверен.

`COMPLETE_WITH_BLOCKERS` допустим только если вся доступная работа выполнена, все blockers конечны и очередь Spark не содержит задач, зависящих от недоказанного контракта. В таком случае `spark/READY` можно создать лишь для строго отделённых READY-задач; в `ASSURANCE_STATEMENT.md` и `START_HERE_FOR_SPARK.md` должен быть крупный перечень ограничений. При существенной незавершённости marker запрещён.

---

## 21. Итоговые документы

`COMBINED_EXECUTIVE_SUMMARY.md`:

- RUN_ID, source/current commit, branch и environment;
- фактический scope PHASE A + PHASE B;
- counts features/requirements/scenarios/tests;
- PASS/FAIL/BLOCKED/INCONCLUSIVE;
- findings по severity/status;
- top systemic root causes;
- verdict по profiles/models, offline, GUI, agent/tools, persistence, security, performance;
- READY/BLOCKED Spark tasks;
- крупнейшие residual risks.

`ASSURANCE_STATEMENT.md` должен отвечать:

> Что именно о качестве JARVIS теперь подтверждено, в каких условиях, какими evidence и чего аудит всё ещё не гарантирует?

Запрещены рекламные заявления вроде «ошибок больше нет» или «проверены абсолютно все состояния».

`RESIDUAL_RISK.md`:

- blocked/unsafe/unavailable tests;
- platform/version variants;
- nondeterministic areas;
- external dependencies;
- soak limits;
- spec gaps;
- assumptions;
- combinations outside tested strength;
- accepted risks.

Обнови `PIPELINE_STATE.json`:

```json
{
  "phase_b": {
    "status": "COMPLETE | COMPLETE_WITH_BLOCKERS | INCOMPLETE",
    "executor": "Sol Ultra / local Codex",
    "live_machine_access": true,
    "finished_at_utc": "..."
  },
  "spark": {
    "status": "READY | PARTIALLY_READY | LOCKED",
    "ready_tasks": 0,
    "blocked_tasks": 0
  }
}
```

---

## 22. Финальный ответ Codex

Не выгружай весь аудит в чат. Сначала сохрани и проверь артефакты. Затем сообщи:

- путь из `.audit/LATEST_COMPLETE_RUN.txt` или точную причину его отсутствия;
- source/current commit и source drift;
- проверенные профили и доказанное model mapping;
- число features/scenarios и распределение результатов;
- findings по severity/status;
- количество READY/BLOCKED Spark tasks;
- пять наиболее опасных подтверждённых проблем;
- крупнейшие blockers/residual risks;
- точный путь к `spark/START_HERE_FOR_SPARK.md`;
- создан ли `spark/READY`;
- точный cleanup/state системы после аудита.

Красивое резюме без реально выполненных live-сценариев, evidence, конечной матрицы, traceability и атомарной очереди Spark считается провалом PHASE B.
