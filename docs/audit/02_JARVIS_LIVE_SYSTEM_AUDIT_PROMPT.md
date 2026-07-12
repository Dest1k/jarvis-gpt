# JARVIS — PHASE B: ПОЛНЫЙ АУДИТ НА ЖИВОЙ МАШИНЕ И ФИНАЛИЗАЦИЯ ОЧЕРЕДИ SPARK

Ты работаешь как главный инженер по качеству, системный архитектор, SRE, специалист по безопасности, adversarial tester и координатор независимых субагентов. Это **вторая половина двухкомпонентного доказательного аудита JARVIS**.

PHASE A уже должна была исследовать репозиторий в облачном Work без доступа к живой машине и сохранить карту системы, контракты, статические findings, hermetic test results и `LIVE_SCENARIO_QUEUE.csv`. Твоя задача — продолжить тот же RUN_ID на реальной Windows-машине, проверить фактическое поведение готового изделия, подтвердить или опровергнуть статические выводы, найти новые runtime-дефекты и только затем создать окончательную атомарную очередь исправлений для Codex Spark.

Это не просьба просто запустить `doctor.ps1`, существующий test suite или один smoke. Проведи систематическую испытательную кампанию по всей обнаруженной поверхности, включая нормальные, граничные, ошибочные, конкурентные, ресурсно-напряжённые, повреждённые и враждебные состояния. При этом не обещай математически невозможное «отсутствие неизвестных ошибок»: докажи максимально возможное покрытие и честно зафиксируй остаточный риск.

---

## 0. Фиксированная схема каталогов

На целевой машине:

```text
D:\jarvis-gpt   — единственный рабочий Git-репозиторий и исходный код
D:\jarvis       — отдельное хранилище моделей, Docker/runtime-данных,
                  пользовательских данных, кешей, больших логов и evidence
```

Начни строго так:

```powershell
Set-Location D:\jarvis-gpt
git -C D:\jarvis-gpt rev-parse --show-toplevel
```

Ожидаемый Git root — `D:/jarvis-gpt` с допустимым различием регистра и разделителей.

`D:\jarvis` **не является репозиторием**. Не создавай там `.audit`, не редактируй там исходники и не считай его рабочим деревом. Большие evidence-файлы разрешено размещать только в:

```text
D:\jarvis\audit-evidence\<RUN_ID>\
```

Если `D:\jarvis-gpt` недоступен или не является Git-репозиторием, зафиксируй точный blocker. Не подменяй его `D:\jarvis`.

---

## 1. Сначала найди и проверь PHASE A

Прочитай:

```text
.audit/LATEST_STATIC_RUN.txt
<run>/PIPELINE_STATE.json
<run>/SOURCE_BASELINE.json
<run>/AUDIT_STATE.md
<run>/STATIC_EXECUTIVE_SUMMARY.md
<run>/SYSTEM_MAP.md
<run>/FEATURE_CATALOG.md
<run>/BEHAVIORAL_CONTRACT.md
<run>/STATIC_FINDINGS_INDEX.md
<run>/LIVE_AUDIT_PLAN.md
<run>/LIVE_SCENARIO_QUEUE.csv
<run>/handoff/PHASE_B_START_HERE.md
```

Проверь:

- `phase_a.status` равен `COMPLETE` или `COMPLETE_WITH_BLOCKERS`;
- run path существует;
- source commit существует в локальной истории;
- ссылки и machine-readable artifacts читаются;
- production-код PHASE A не был молча изменён.

Если PHASE A отсутствует или неполна:

- не притворяйся, что handoff есть;
- сохрани blocker;
- восстанови минимально необходимую карту/контракты/feature inventory локально read-only;
- продолжай runtime-аудит настолько полно, насколько возможно;
- явно пометь происхождение артефактов и недостающую независимость первой фазы.

Не задавай пользователю вопрос, если можешь безопасно продолжить с зафиксированным допущением.

---

## 2. Проверка source drift

Сравни текущий production tree с `source_commit` из PHASE A, исключив только audit prompt/artifact paths:

```text
.audit/**
docs/audit/**
```

Создай `SOURCE_DRIFT.md` и зафиксируй:

- текущий branch/HEAD;
- commits после source commit;
- changed production files;
- какие FEAT/REQ/SCN/findings затронуты;
- что нужно переанализировать.

Если изменились только `.audit/**` и `docs/audit/**`, продолжай тот же RUN_ID.

Если production drift мал и локализован, продолжай тот же run, но повтори статический анализ затронутых путей и обнови contracts/scenarios.

Если drift велик либо source commit не является предком текущего HEAD, создай derived run:

```text
<OLD_RUN_ID>__live_<UTC>_<current short SHA>
```

Сохрани ссылку `derived_from` в `PIPELINE_STATE.json` и перенеси только проверенные артефакты. Не применяй старые PASS/findings к новому коду автоматически.

---

## 3. Установка по-прежнему вне scope

Не проверяй:

- чистую машину;
- первичное скачивание проекта;
- первичное получение моделей/образов;
- installer/bootstrap с нуля;
- выбор способа установки.

Считай, что изделие уже установлено и базово работоспособно.

Проверяй весь эксплуатационный lifecycle готового продукта:

- cold/warm start из подготовленного состояния;
- stop/restart/repeated start;
- частично запущенный стек;
- crash/recovery;
- offline-first;
- config changes;
- corrupted/stale/locked runtime state;
- resource pressure;
- concurrency/cancellation/retry;
- длительную работу;
- точную диагностику и безопасное восстановление.

Не маскируй дефект советом «переустановить». Сначала докажи root cause и expected recovery.

---

## 4. Правила безопасной работы

1. Не исправляй production-код во время аудита.
2. Не перезаписывай пользовательские данные, модели, реальные секреты, рабочие volumes или незакоммиченные изменения.
3. Никогда не используй `git reset --hard`, агрессивный `git clean`, массовый Docker prune, удаление реальных volumes, сброс WSL или необратимые host-команды.
4. Destructive/corruption/disk-full/permission/chaos tests выполняй на изолированных копиях, test databases, test directories, отдельном Compose project name, временных volumes и синтетических данных.
5. Перед остановкой Docker/WSL/host service проверь, нет ли посторонних workloads. Если изоляция невозможна, симулируй fault или поставь `BLOCKED_BY_SAFETY`.
6. Не исполняй опасные команды, сгенерированные LLM, против реальной системы. Используй sandbox/dry-run/allowlist/synthetic targets.
7. Security tests направляй только на локальный JARVIS и принадлежащие проекту тестовые ресурсы.
8. Реальные секреты/PII маскируй. Используй синтетические canary secrets для leakage tests.
9. Не устанавливай/обновляй runtime продукта. Временные audit tools — только изолированно, с журналом и без изменения production environment.
10. Не push-ь и не открывай PR. В конце можно создать один **локальный** commit только с `.audit/**`, если это не захватывает чужие изменения.
11. Stateful, GPU-heavy, Docker-mutating, GUI и chaos tests выполняй последовательно. Read-only анализ и полностью изолированные tests можно распараллеливать.
12. Любой workaround отделяй от production state и явно помечай результаты, полученные с ним.
13. Красный тест — результат, а не повод прекратить остальные независимые направления.

---

## 5. Продолжай тот же каталог результатов

Работай в run-каталоге PHASE A. Добавь/обнови:

```text
<run>/
  PIPELINE_STATE.json
  AUDIT_STATE.md
  SOURCE_DRIFT.md
  LIVE_BASELINE.md
  LIVE_ENVIRONMENT.json
  LIVE_TEST_RESULTS.md
  RUNTIME_SCENARIO_MATRIX.csv
  SCENARIO_MATRIX.csv
  TRACEABILITY.csv
  COVERAGE_REPORT.md
  FINDINGS_INDEX.md
  EXECUTIVE_SUMMARY.md
  ASSURANCE_STATEMENT.md
  BLOCKERS.md
  RESIDUAL_RISK.md
  AUDIT_JOURNAL.md
  EVIDENCE_MANIFEST.json
  findings/
  repros/
  harness/
    live/
  evidence/
    live/
  workstreams/
  spark/
    START_HERE_FOR_SPARK.md
    SPARK_MASTER_PROMPT.md
    SPARK_QUEUE.csv
    SPARK_PROGRESS.md
    TASK_SCHEMA.md
    READY
    tasks/
      SPARK-0001.md
  machine/
    scenario_results.jsonl
    findings.jsonl
    spark_tasks.jsonl
```

Лёгкие текстовые артефакты и короткие sanitized logs — внутри repo `.audit`.

Большие logs/screenshots/video/dumps/traces/profiles:

```text
D:\jarvis\audit-evidence\<RUN_ID>\
```

В `EVIDENCE_MANIFEST.json` для каждого внешнего файла укажи:

- evidence ID;
- absolute path;
- size;
- SHA-256;
- MIME/type;
- created_at UTC;
- related scenario/finding;
- sanitization status;
- capture command/settings.

---

## 6. Статусы PHASE B

Для runtime scenarios:

- `PASS` — реально выполнен, oracle проверен, evidence сохранён;
- `FAIL` — реально выполнен и нарушил contract/invariant;
- `BLOCKED` — конкретный внешний/безопасностный blocker;
- `NOT_APPLICABLE` — доказуемо неприменим;
- `INCONCLUSIVE` — данные противоречивы/nondeterministic;
- `NOT_RUN` — ещё не выполнен; допустим только до завершения кампании.

Не конвертируй `STATIC_SUPPORTED` из PHASE A в `PASS` без живого выполнения, если поведение runtime.

Для findings PHASE A обновляй:

- `confirmed-runtime`;
- `confirmed-static-only`;
- `refuted-runtime`;
- `probable`;
- `blocked`;
- `spec-gap`;
- `test-gap`.

Каждый confirmed defect воспроизведи минимум дважды из именованного исходного состояния, если это безопасно. Intermittent behavior измеряй серией, а не впечатлением.

---

## 7. Организация Ultra/subagents

Рекомендуемые независимые workstreams:

1. Windows/PowerShell/launcher/config;
2. Docker/WSL/profiles/models/offline;
3. backend/API/WebSocket/storage;
4. LLM/provider/agent/missions/tools/approvals;
5. memory/persona/learning/autonomy/self-heal;
6. web/browser/documents/files;
7. frontend/GUI/accessibility/service worker;
8. concurrency/fault injection/recovery/data integrity;
9. security/privacy;
10. performance/resources/soak;
11. независимый adversarial verifier.

Главный агент:

- выдаёт stateful resources по одному;
- не позволяет двум субагентам одновременно менять Docker/GPU/runtime state;
- проверяет primary evidence;
- дедуплицирует findings;
- обновляет `AUDIT_STATE.md` после каждого блока;
- сохраняет exact next command для восстановления после compaction.

---

## 8. PHASE B0 — live baseline

Зафиксируй:

- Git branch/HEAD/status;
- Windows version/build;
- PowerShell;
- WSL distributions/version/state;
- Docker Desktop/Engine/Compose;
- Python/Node/npm и используемые runtimes;
- GPU/driver/CUDA/VRAM;
- CPU/RAM/disks/free space;
- модели и их paths/sizes/checksums where feasible;
- Docker images/containers/networks/volumes только относящиеся к JARVIS;
- processes/ports/services;
- current env/profile/config resolution с redaction;
- persistent stores and sizes;
- baseline resource usage;
- current application readiness.

Запусти минимальный read-only smoke готового изделия и сохрани immutable baseline artifacts. Не меняй baseline после fault tests.

Выведи фактический список profiles из launcher/config/CLI/runtime. Не навязывай старый список. Для каждого profile зафиксируй intended model/config and observed runtime identity.

---

## 9. PHASE B1 — выполнение очереди PHASE A

Открой `LIVE_SCENARIO_QUEUE.csv` и исполни строки по dependency/risk order.

Для каждой строки:

1. восстанови exact preconditions;
2. проверь safety isolation;
3. зафиксируй state before;
4. выполни exact command/action;
5. сохрани stdout/stderr/events/screenshots/telemetry;
6. проверь oracle/invariants;
7. повтори согласно repeat_count;
8. минимизируй failing input/sequence;
9. выполни cleanup;
10. проверь state after и возможность восстановления;
11. обнови matrix/findings/traceability.

Если постановка PHASE A неверна, исправь её в artifacts и объясни почему. Не сохраняй устаревшую гипотезу ради согласия между моделями.

---

## 10. PHASE B2 — launcher, profiles, Docker, WSL и offline-first

Для **каждого фактически поддерживаемого profile** проверь независимо:

- штатный start/stop/restart;
- cold и warm start;
- повторный start при already-running;
- start из другого cwd;
- paths с пробелами/кириллицей/длинным именем там, где безопасно;
- unknown profile/typo/empty/malformed env;
- common vs profile-specific env precedence;
- stale PID/lock/container/network;
- occupied ports;
- partial stack;
- падение/перезапуск каждого service;
- startup ordering/readiness race;
- healthcheck false positive/negative;
- process/container/port/GPU cleanup;
- ordinary user и permission-denied diagnostics;
- repeated start/stop/restart cycles;
- growth of containers/networks/volumes/logs/temp/cache.

Докажи фактическую model identity не только env-переменной:

- resolved launcher/profile config;
- Compose-rendered command/env;
- container/runtime metadata;
- provider `/models`/health metadata where available;
- startup log/model path;
- observable served model identity.

### Offline-first

После подготовленного online baseline:

- блокируй/отключай сеть безопасным обратимым способом;
- блокируй DNS и registry отдельно, если возможно;
- запускай локальное ядро без pull/resolve/download/package calls;
- наблюдай реальные DNS/TCP/HTTP/registry обращения;
- проверяй отсутствие зависания на remote metadata;
- проверяй локальный chat/GUI/state;
- проверяй сетевые tools: они должны падать локально, понятно и не рушить систему;
- восстанавливай сеть и проверяй recovery.

Чтение `pull_policy=never` не является доказательством. Нужна наблюдаемая сетевой трасса/отсутствие обращений.

Минимум 10 безопасных repeated start/stop cycles на основном profile, если среда выдерживает; для остальных — минимум 3. Любое сокращение документируй.

---

## 11. PHASE B3 — backend, API, NDJSON stream и WebSocket

Для каждого endpoint/event contract из FEATURE_CATALOG:

- happy path;
- missing/extra/wrong-type/malformed payload;
- empty/boundary/oversized input;
- Unicode/Cyrillic/emoji/control chars/long unbroken text;
- stable error schema и user-facing message;
- timeout/cancel/retry/idempotency;
- abrupt client disconnect;
- server restart mid-request/stream;
- duplicate/out-of-order/late/missing terminal events;
- half-open/reconnect/multiple connections;
- slow consumer/backpressure;
- concurrent sessions;
- CORS/origin/bind/auth/session isolation;
- no silent success;
- no secret/traceback leakage.

Используй schema-aware fuzzing с bounded budget и seed. Для критичных serializers/parsers — не менее нескольких сотен cases, если быстро; увеличивай budget до практического предела без перегрузки. Сохраняй минимальные failing payloads.

Для stream проверь:

- token/event order;
- exactly one terminal outcome;
- cancel semantics;
- UI/backend state reconciliation;
- reconnect behavior;
- partial message persistence;
- late event after session close;
- repeated retry without duplicated side effects.

---

## 12. PHASE B4 — LLM/provider, agent core, missions и tools

Разделяй:

1. deterministic orchestration tests с fake/mock provider;
2. smoke/behavior tests с реальной локальной model.

Не требуй exact wording от nondeterministic LLM. Используй semantic contracts, schemas, invariants, tool side effects и bounded rubrics.

Проверь:

- empty/tiny/large/near-context-limit prompts;
- Russian/English/mixed Unicode/emoji/code/Markdown/JSON/XML/shell output;
- malformed/partial/duplicate/out-of-order model chunks;
- timeout/disconnect/slow tokens/empty response/invalid finish reason;
- invalid tool name/args/types/malformed JSON;
- multiple dependent tool calls;
- partial success and rollback/diagnostics;
- tool timeout/cancel/retry/idempotency;
- huge/binary-like/terminal-escape output;
- planner/executor disagreement;
- runaway loop/retry/step limit;
- provider/tool unavailable fallback;
- prompt injection from user/file/web/tool/memory;
- requests to reveal secrets or escape allowed roots;
- shell/PowerShell quoting/injection through arguments;
- cancel before/during/after side effect;
- repeated request after cancel/error;
- multi-session isolation;
- mission run/resume/report consistency;
- trace UI matching actual actions;
- self-verification result integrity;
- no fabricated success after model/tool failure.

Для каждого registered tool создай минимальную runtime matrix:

```text
success, invalid input, permission denied, timeout, cancel,
partial result, huge result, retry, concurrent use,
unsafe request, encoding/path edge case
```

Опасные tools — только synthetic targets/sandbox/approval tests.

---

## 13. PHASE B5 — approvals, host bridge, Windows native, Docker policy и self-heal

Проверь:

- explicit user command получает только argument-bound permission;
- незапрошенное dangerous action создаёт durable approval;
- approval нельзя переиспользовать для другого action/args/session;
- reject/cancel/expiry/restart semantics;
- duplicate/replayed approval event;
- UI accurately displays pending/completed/failed state;
- host bridge token/auth and bind exposure;
- ordinary read-only inspect vs mutating action boundary;
- command injection/quoting/path validation;
- Docker policy ограничивает scope JARVIS resources;
- self-heal только предлагает безопасные/approval-gated actions;
- autonomy jobs respect budgets, policies and stop conditions;
- crash/restart не приводит к silent execution;
- audit trail полно отражает actor/action/args/result.

Используй canary actions без реального ущерба.

---

## 14. PHASE B6 — memory, persona, learning, jobs и persistent data

На изолированной test DB/copy проверь:

- create/read/update/delete where supported;
- WAL/lock contention;
- concurrent writes;
- partial write/process kill;
- read-only/unavailable/nearly-full storage;
- corrupt copy/schema mismatch/stale index;
- migration behavior текущей установленной базы без установки с нуля;
- duplicate event/idempotency;
- restart with in-flight mission/tool/job;
- memory retrieval relevance and isolation;
- persona stable facts/dedup/caps/deletion;
- prompt injection/poisoning via learned content;
- experience feedback affects only intended context;
- background supervisor/jobs do not race or grow unbounded;
- clock jump/timezone/expiry boundaries;
- logs/history/cache/index retention and growth;
- synthetic secret/PII redaction/deletion.

После каждого fault докажи recovery или сохрани failure evidence. Никогда не corrupt реальную пользовательскую DB; только проверенную копию.

---

## 15. PHASE B7 — web, browser, shopping, weather, watch и evidence

В online и offline состояниях проверь фактические registered tools:

- search/fetch/render/archive/feed/weather/watch/shop routes;
- source provenance/citations/URLs;
- cache freshness/staleness labeling;
- timeout/cancel/retry/cooldown;
- redirects/blocked pages/fallbacks;
- malformed/huge content;
- prompt injection from pages;
- SSRF/private/local targets в безопасной лабораторной конфигурации;
- DNS rebinding protections where feasible;
- download quarantine/MIME/archive paths;
- browser approval, tab/session isolation and human handoff;
- external outage не ломает local core;
- offline error быстро и понятно возвращается;
- stale cached answer не выдаётся за свежий;
- named-shop constraints/ranking/provenance;
- watch lifecycle and duplicate notifications;
- evidence ledger/extract/verify integrity.

Не атакуй сторонние сайты. Для adversarial web content подними локальный test server/synthetic pages.

---

## 16. PHASE B8 — documents, files и directory ingestion

Используй только синтетический corpus и копии:

- text/Markdown/JSON/CSV;
- DOCX/XLSX/PPTX/PDF/RTF/ODT, если реально поддерживаются;
- empty/minimal/large/Unicode/malformed/truncated files;
- extension/MIME mismatch;
- malicious filenames/path traversal/archive-slip;
- long paths, spaces, Cyrillic;
- formulas/styles/tables/images/external links;
- scanned PDF/OCR-needed state;
- duplicate names and versioning;
- copy-on-write edit/generate/convert;
- original never overwritten;
- output collision and cleanup;
- parser crash isolation;
- chunk/index/retrieval consistency;
- persisted recall after restart;
- prompt injection inside documents;
- directory file/count/size limits;
- allowed-root enforcement;
- huge/decompression-bomb protections.

Проверь не только API, но и UI upload/inspect/review/search flows.

---

## 17. PHASE B9 — Command Center GUI и service worker

Проверь GUI функционально и визуально:

- first load/empty state;
- chat first request and long history;
- streaming/cancel/retry;
- tool/mission/approval progress;
- backend/model/tool failure;
- offline/reconnect;
- reload/history restore;
- profile/model/status truthfulness;
- files/memory/tools/audit/diagnostics panels;
- persona/briefing/autonomy/self-heal/benchmark surfaces;
- document upload/review;
- WebSocket live updates;
- trace page;
- service-worker cached shell and invalidation;
- voice input graceful availability/failure.

Viewport/window coverage where environment allows:

- minimum reasonable size;
- 1280×720;
- 1920×1080;
- ultrawide equivalent;
- DPI/scale 100/125/150/200%;
- maximize/restore/resize during stream;
- long text/code/table/URL/unbroken word;
- Cyrillic/emoji/mixed direction;
- scroll anchoring/focus;
- Enter/Shift+Enter/Escape/keyboard navigation;
- copy/paste/selection;
- accessibility labels/focus/contrast/semantics;
- no overlap/clipping/layout jumps/double submit/stale state;
- no raw traceback or internal legacy branding.

Каждый visual finding должен иметь screenshot, viewport, DPI, state, exact steps и oracle.

---

## 18. PHASE B10 — concurrency, fault injection, recovery и data integrity

Проверь:

- several requests same/different sessions;
- parallel tool calls;
- cancel racing terminal event;
- backend restart during stream;
- frontend/WS/model/tool worker failure;
- process kill during state write on test copy;
- lock contention;
- delayed/out-of-order/duplicate delivery;
- disk nearly full/write failure in isolated target;
- read-only directory;
- temporary store unavailability;
- partial/corrupt config/cache/index/session/log copy;
- clock jump/timeout boundary;
- crash resume/recovery;
- rapid repeated start/stop/restart;
- orphan task/process/container/GPU allocation;
- retry storms and thundering herd;
- cleanup after failed startup.

Для critical state machines применяй model-based/seeded random sequences. Сохраняй seed и minimized sequence.

Race-prone scenarios повторяй минимум 20 раз либо до статистически убедительного результата в пределах безопасного бюджета; фиксируй fail count/total, а не единичный исход.

---

## 19. PHASE B11 — security и privacy

Актуализируй threat model PHASE A и выполни безопасные PoC/tests:

- command/shell/PowerShell injection;
- arbitrary file read/write;
- traversal/junction/symlink confusion;
- prompt injection from all untrusted channels;
- tool permission/approval bypass;
- unsafe deserialization/schema bypass;
- API/WS bind/CORS/auth/session isolation;
- SSRF to synthetic local targets;
- secret leakage in logs/UI/errors/env/model/audit;
- unsafe defaults/broad filesystem/Docker permissions;
- dependency/image pinning and vulnerability database freshness;
- malicious filenames/archive paths/oversized inputs;
- log injection/terminal escape;
- retry of irreversible action;
- action attribution spoofing;
- browser/host bridge token boundaries;
- cross-session/cross-user data leakage.

Используй synthetic secrets and canary paths. Не публикуй working exploit против внешних systems; PoC должен быть минимальным и локальным.

---

## 20. PHASE B12 — performance, resources и soak

Для каждого meaningful profile измерь сериями:

- cold/warm startup and time-to-ready;
- first-token and completion latency типовых запросов;
- API/UI latency without/with LLM;
- CPU/RAM/VRAM/disk/network;
- model loading and cleanup;
- cancel/shutdown resource release;
- repeated cycle leaks;
- queue/backpressure under concurrency;
- low free disk/memory within safe bounds;
- long conversation/history;
- logs/cache/DB/index growth;
- frontend responsiveness during streaming;
- browser/document workloads.

Отделяй warmup от steady state. Сохраняй raw measurements, median/p95/range и environment.

Если официальных SLO нет, не выдумывай threshold. Зафиксируй baseline, anomalies and practical limits.

Проведи bounded soak не менее 60 минут для основного profile, если нет blocker, с периодическими chat/tool/memory/WS actions и resource snapshots. Для невозможного longer soak укажи residual risk.

---

## 21. Методы поиска неизвестных runtime-ошибок

Помимо ручных сценариев используй, где разумно:

- property-based testing;
- schema/grammar-aware fuzzing;
- stateful/model-based testing;
- metamorphic testing;
- differential testing между profiles/fallbacks;
- mutation testing качества tests;
- fault injection/chaos;
- seeded random action sequences with shrinking;
- log/trace mining;
- repeated-run flaky/race detection;
- independent adversarial review of every high-risk PASS.

Не запускай uncontrolled fuzz/chaos. Используй watchdogs, disk quotas, request limits и cleanup.

---

## 22. Findings: объединение PHASE A и PHASE B

Не создавай дубликат только потому, что runtime подтвердил static finding. Обнови исходный `JARVIS-NNNN.md`:

```yaml
phase_b_status: confirmed-runtime | confirmed-static-only | refuted-runtime | probable | blocked | spec-gap | test-gap
runtime_reproducibility: always | frequent | intermittent | once | not-run | not-applicable
runtime_evidence_ids: []
root_cause_status: confirmed | strong-hypothesis | unknown
spark_task_ids: []
```

Для нового runtime finding используй следующий свободный ID.

Обязательные разделы final finding:

1. Summary.
2. User/system impact.
3. Exact environment/preconditions.
4. Reproduction from named baseline.
5. Expected result.
6. Actual result.
7. Evidence paths/excerpts.
8. Frequency/series statistics.
9. Root cause status.
10. Affected code/config paths and symbols.
11. Security/data-loss implications.
12. Safe workaround.
13. Minimal remediation direction.
14. Regression risks.
15. Binary acceptance criteria.
16. Tests that fail before/pass after.
17. Related findings/spec/test gaps.

Severity rubric:

- `critical`: arbitrary dangerous action/RCE, secret exposure, irreversible data loss, severe trust-boundary failure;
- `high`: main flow broken, wrong model/profile, data corruption, unreliable recovery, substantial security boundary failure;
- `medium`: meaningful functional/recovery/performance/UX defect;
- `low`: limited edge/cosmetic/diagnostic/documentation issue.

---

## 23. Подготовка финальных задач для Spark

После объединения findings создай атомарные задачи. У пользователя нет второго тяжёлого ремонтного исполнителя, поэтому Spark не должен заново проводить весь аудит или принимать скрытые архитектурные решения.

### Декомпозиция

- одна задача = один defect/root cause/preparatory test step;
- обычно одна subsystem и не более пяти production files;
- большие fixes разделяй на investigation/contract/test-foundation/implementation/validation;
- сначала tests/observability/contracts, затем fixes, затем cleanup;
- не создавай «исправь backend», «улучши надёжность», «разберись с Docker»;
- укажи dependencies/conflicts/file overlap;
- ambiguous behavior → `BLOCKED_BY_SPEC`;
- environment-only blocker → `BLOCKED_BY_ENV`;
- сложную работу дроби до последовательности, которую Spark способен выполнить;
- каждая задача должна быть самодостаточной без всего audit context.

### `spark/tasks/SPARK-NNNN.md`

```yaml
id: SPARK-0001
title: "..."
source_findings: [JARVIS-0001]
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

Обязательное содержимое:

1. Goal.
2. Why this matters.
3. Known-good baseline.
4. Reproduction before change with exact expected FAIL.
5. Observed evidence.
6. Confirmed root cause or explicit hypothesis.
7. Relevant code map.
8. Implementation constraints.
9. Minimal fix strategy.
10. Test-first requirement.
11. Exact validation commands.
12. Binary acceptance criteria.
13. Regression checklist.
14. Stop/escalation conditions.
15. Expected task report.
16. Commit message suggestion.

Каждая task содержит предупреждение:

> Не объявляй задачу выполненной по чтению кода. Сначала воспроизведи FAIL, добавь или обнови regression test, выполни минимальный patch, запусти все указанные проверки и проверь git diff на несвязанные изменения.

### Queue

`spark/SPARK_QUEUE.csv`:

```text
order,task_id,priority,status,batch,depends_on,conflicts_with,risk,
scope,profiles,components,expected_tests,notes
```

Порядок:

1. test/observability foundations;
2. P0;
3. P1;
4. shared root causes;
5. medium/low;
6. cleanup/docs;
7. batch validation;
8. final full regression.

После каждого batch создай validation task. Не планируй параллельно задачи с общими files/state/GPU/runtime.

### Generated Spark operator files

Создай:

- `spark/START_HERE_FOR_SPARK.md` — короткая инструкция человеку;
- `spark/SPARK_MASTER_PROMPT.md` — полный режим работы Spark;
- `spark/SPARK_PROGRESS.md` — очередь/статусы/последний commit/next task;
- `spark/TASK_SCHEMA.md`;
- `spark/READY` — только после terminal completion PHASE B.

---

## 24. Traceability и полнота

Финальный `TRACEABILITY.csv`:

```text
feature -> requirement/invariant -> scenario -> method -> result
        -> evidence -> finding -> Spark task
```

Consistency checker должен обнаруживать:

- feature без contract/spec-gap;
- high-risk requirement без positive/negative/recovery scenario;
- scenario без terminal status;
- FAIL без finding;
- confirmed finding без task или `NO_CODE_CHANGE` rationale;
- task без source finding/acceptance;
- P0/P1 потерянный между finding и queue;
- broken ID/path;
- Spark READY при незавершённой PHASE B.

Запусти checker и сохрани result.

---

## 25. Условия завершения объединённого аудита

Не называй PHASE B завершённой, пока:

1. обработан handoff PHASE A и source drift;
2. построен live baseline;
3. каждый фактический profile реально проверен либо конкретно BLOCKED;
4. model/profile identity доказана по всей цепочке;
5. offline-first проверен сетевым наблюдением;
6. все штатные tests и available static/live checks запущены;
7. каждая public feature имеет happy/error/recovery coverage либо explicit gap;
8. high-risk state transitions/concurrency/cancel/retry/crash/recovery выполнены либо blocked;
9. property/fuzz/model-based/fault-injection методы применены к critical surfaces;
10. threat model связан с реальными tests;
11. GUI проверен функционально и визуально;
12. performance/resource/repeated-cycle/soak results сохранены;
13. все scenario rows имеют terminal status; `NOT_RUN` означает незавершённость;
14. findings дедуплицированы и имеют evidence/root cause confidence;
15. PHASE A findings подтверждены/опровергнуты/заблокированы;
16. confirmed defects преобразованы в atomic Spark tasks;
17. queue имеет dependencies/batches/validation checkpoints;
18. traceability checker проходит;
19. residual risks/blockers/spec gaps честно перечислены;
20. production code не был исправлен во время аудита.

Если среда не позволяет выполнить всё, допускается `COMPLETE_WITH_BLOCKERS` только при условии, что все доступные проверки завершены, а каждое недоказанное утверждение отражено в `RESIDUAL_RISK.md`. `NOT_RUN` rows должны быть преобразованы в `BLOCKED` с конкретной причиной либо аудит остаётся `INCOMPLETE`.

---

## 26. Разблокировка Spark

Только при `phase_b.status = COMPLETE` или `COMPLETE_WITH_BLOCKERS`:

1. обнови `PIPELINE_STATE.json`:

```json
{
  "phase_b": {
    "status": "COMPLETE | COMPLETE_WITH_BLOCKERS",
    "executor": "Sol Ultra / local Codex",
    "finished_at_utc": "..."
  },
  "spark": {
    "status": "READY",
    "ready_tasks": 0,
    "blocked_tasks": 0
  }
}
```

2. создай `<run>/spark/READY` с run ID, audited HEAD и timestamp;
3. запиши относительный run path в:

```text
.audit/LATEST_COMPLETE_RUN.txt
.audit/LATEST_RUN.txt
```

4. убедись, что `.audit/LATEST_STATIC_RUN.txt` остаётся исторической ссылкой;
5. создай локальный commit только с audit artifacts, если безопасно:

```text
audit: complete repository and live-system review <RUN_ID>
```

Не push.

Если PHASE B `INCOMPLETE`, не создавай READY и не записывай `LATEST_COMPLETE_RUN.txt`.

---

## 27. Финальные документы

`EXECUTIVE_SUMMARY.md`:

- source/live commit and environment;
- scope;
- counts of features/requirements/scenarios/tests;
- PASS/FAIL/BLOCKED/INCONCLUSIVE;
- findings by severity/status;
- systemic root causes;
- P0/P1;
- profile/model/offline verdicts;
- GUI/agent/tools/data/security/performance verdicts;
- Spark queue readiness.

`ASSURANCE_STATEMENT.md` отвечает:

> Что именно теперь можно утверждать о качестве JARVIS, в каких условиях и на основании каких доказательств?

Отдельно перечисли невозможные гарантии.

`RESIDUAL_RISK.md`:

- blocked/unavailable scenarios;
- nondeterministic areas;
- platform/version variants;
- longer soak needs;
- external dependency risks;
- spec/test gaps;
- assumptions;
- unknown combination risk.

---

## 28. Финальный ответ Codex

Не выгружай весь аудит в чат. Сообщи:

- путь из `.audit/LATEST_COMPLETE_RUN.txt` либо почему он не создан;
- audited source/HEAD and profiles;
- features/scenarios and result distribution;
- findings by severity;
- READY/BLOCKED Spark tasks;
- пять наиболее опасных confirmed problems;
- главные blockers/residual risks;
- путь к `<run>/spark/START_HERE_FOR_SPARK.md`;
- local audit commit, если создан;
- явный verdict: Spark разблокирован или нет.

Красивый summary без выполненной runtime-кампании, evidence, traceability и атомарной queue считается провалом PHASE B.
