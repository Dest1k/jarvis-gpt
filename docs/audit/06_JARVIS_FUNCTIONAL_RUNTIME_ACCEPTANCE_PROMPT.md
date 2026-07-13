# JARVIS — ФУНКЦИОНАЛЬНАЯ ПРОВЕРКА ГОТОВОГО ИЗДЕЛИЯ НА ЖИВОЙ МАШИНЕ

Ты выполняешь отдельную, полностью функциональную проверку принадлежащего пользователю локального проекта JARVIS через Codex Sol Ultra.

Главная цель:

> Доказать, что в обычной работе JARVIS понимает задачу оператора, соблюдает явные инструкции, реально выполняет доступные действия, выдаёт законченный полезный результат, честно сообщает о недоступности и не показывает пользователю внутренние технические фрагменты вместо ответа.

Также проверь запуск, профили, GUI, streaming, документы, tools, missions, memory, ошибки, восстановление, производительность и длительную обычную работу.

Это **не продолжение расширенного review границ доступа**. Не загружай и не исполняй старую полную очередь, из-за которой предыдущие сессии останавливались интерфейсом. Работай только по этому документу и по обычным пользовательским сценариям ниже.

Все сообщения пользователю — только на русском. Подробности сохраняй в `.audit/**`; в чат выводи только процент готовности, краткие counts, paths и реальные blockers. Плановые отчёты — на 50% и 90%.

---

## 1. Каталоги

```text
D:\jarvis-gpt   — Git-репозиторий и audit artifacts
D:\jarvis       — модели, runtime-данные, backups и тяжёлые evidence
```

Начни:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
git status --short --branch
git rev-parse HEAD
```

Ожидаемый Git root — `D:/jarvis-gpt`.

Не используй `git reset --hard`, aggressive clean, auto-stash, rebase или присвоение пользовательских изменений.

---

## 2. Синхронизация prompt-файлов без потери текущих artifacts

1. Зафиксируй текущий status и список modified/untracked файлов.
2. Проверь наличие `.audit/LATEST_STATIC_RUN.txt`.
3. Выполни `git fetch`.
4. Разрешён только обычный fast-forward текущей ветки при отсутствии конфликтующих tracked-изменений.
5. Не удаляй и не перезаписывай существующие `.audit/**`.
6. Если fast-forward невозможен, остановись с точным blocker; не используй reset/rebase/stash.
7. После синхронизации перечитай этот файл целиком.

Изменения только в `docs/audit/**` не являются production source drift.

---

## 3. Сохранение результата прежних попыток

Предыдущая PHASE B могла оставить частичные изменения в `.audit/**` и фоновые процессы.

До новых проверок:

1. Найди `RUN_ID` через `.audit/LATEST_STATIC_RUN.txt`.
2. Создай внешний checkpoint:

```text
D:\jarvis\audit-backups\<RUN_ID>\pre-functional-resume\
```

3. Сохрани туда:

- список Git status;
- список changed/untracked `.audit` files;
- `git diff -- .audit/**` как patch, если diff доступен;
- копию текущего run-каталога либо manifest + hashes, если полный copy слишком велик;
- список процессов, контейнеров и портов, которые могла создать прежняя попытка.

4. Не отменяй существующие изменения через UI и не удаляй partial artifacts.
5. Останавливай только процесс/fixture, принадлежность которого текущему audit run доказана.
6. Создай в новом functional-каталоге `RESUME_FROM_PARTIAL.md` с описанием сохранённого состояния.

Старые частичные результаты не считаются PASS, пока их evidence не проверен. Они могут быть использованы как подсказка, но не как источник обязательной очереди.

---

## 4. Ограниченный набор входных документов

Для этой функциональной кампании разрешено читать:

```text
.audit/LATEST_STATIC_RUN.txt
.audit/runs/<RUN_ID>/PIPELINE_STATE.json
.audit/runs/<RUN_ID>/SOURCE_BASELINE.json
.audit/runs/<RUN_ID>/FEATURE_CATALOG.md
.audit/runs/<RUN_ID>/BEHAVIORAL_CONTRACT.md
README.md
.env.example
актуальные launcher/config/API/UI/test files по мере необходимости
repository instructions
```

Не загружай целиком и не исполняй:

```text
.audit/runs/<RUN_ID>/LIVE_SCENARIO_QUEUE.csv
.audit/runs/<RUN_ID>/STATIC_FINDINGS_INDEX.md
.audit/runs/<RUN_ID>/findings/**
.audit/runs/<RUN_ID>/DATA_AND_TRUST_BOUNDARIES.md
.audit/runs/<RUN_ID>/ARCHITECTURE_RISKS.md
docs/audit/02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md
docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md
```

Не выполняй старые scenario IDs по памяти. Построй новую нейтральную функциональную очередь из этого prompt и фактического `FEATURE_CATALOG`.

---

## 5. Отдельный functional run

Продолжай исходный `RUN_ID`, но все новые материалы складывай в отдельный namespace:

```text
.audit/runs/<RUN_ID>/functional/
  START_HERE.md
  FUNCTIONAL_STATE.json
  ENVIRONMENT_BASELINE.md
  FEATURE_JOURNEY_MAP.csv
  SCENARIO_QUEUE.csv
  RESULTS.csv
  OPERATOR_TASK_CATALOG.csv
  OPERATOR_ACCEPTANCE_RESULTS.csv
  INSTRUCTION_FOLLOWING_REPORT.md
  RESPONSE_INTEGRITY_REPORT.md
  REAL_WORLD_JOURNEYS_REPORT.md
  PROFILE_AND_MODEL_REPORT.md
  STARTUP_AND_RECOVERY_REPORT.md
  GUI_AND_STREAMING_REPORT.md
  DOCUMENT_AND_TOOL_REPORT.md
  MISSION_MEMORY_REPORT.md
  PERFORMANCE_REPORT.md
  LONG_RUN_REPORT.md
  FUNCTIONAL_FINDINGS_INDEX.md
  FUNCTIONAL_ASSURANCE_STATEMENT.md
  RESIDUAL_GAPS.md
  JOURNAL.md
  findings/
  evidence/
  harness/
  machine/
  spark/
```

Не создавай старые `spark/READY` markers.

В конце этой кампании разрешены только:

```text
.audit/runs/<RUN_ID>/functional/READY
.audit/runs/<RUN_ID>/functional/spark/READY
```

и только при выполнении всех критериев ниже.

---

## 6. Статусы

Используй:

- `PASS` — сценарий реально выполнен и expected result подтверждён;
- `FAIL` — результат не соответствует пользовательскому контракту;
- `BLOCKED_BY_ENV` — отсутствует конкретный ресурс;
- `BLOCKED_BY_SAFETY` — нет проверенной изоляции или возврата состояния;
- `BLOCKED_BY_SPEC` — ожидаемое поведение неоднозначно;
- `INCONCLUSIVE` — результат нестабилен или данные противоречивы;
- `NOT_APPLICABLE` — функция доказуемо неприменима;
- `NOT_RUN` — допустим только пока работа не завершена.

Любой FAIL получает finding и disposition для Spark либо явное `NO_CODE_CHANGE`.

---

## 7. Baseline живой машины

Зафиксируй:

- current branch, HEAD, status и source drift;
- Windows, PowerShell, Python, Node/npm;
- Docker/Compose/WSL versions и состояние;
- CPU/RAM/GPU/VRAM/disks/free space;
- фактические paths моделей, runtime, DB, files, logs и caches;
- процессы, контейнеры, networks, ports и initial state;
- фактические active profiles;
- штатные start/stop/restart/status/doctor commands;
- health/readiness signals;
- текущий profile/model, видимый через launcher, provider, API и GUI.

Не скачивай модели/images/dependencies без отдельной необходимости. Не выполняй broad cleanup.

---

## 8. Карта user-facing функций

Из `FEATURE_CATALOG`, README, UI, CLI и API создай `FEATURE_JOURNEY_MAP.csv`.

Каждая реально доступная user-facing feature должна иметь минимум один обычный пользовательский journey либо точный documented gap.

Минимальные группы:

1. launcher/start/stop/restart/status;
2. profile/model selection;
3. chat и streaming;
4. conversations/history;
5. tools и approvals в обычном использовании;
6. files/documents;
7. web/browser только для обычных разрешённых задач;
8. missions/planning/final report;
9. memory/persona/learning;
10. diagnostics/telemetry/benchmark;
11. autonomy/routines/self-heal в заявленном безопасном режиме;
12. Command Center panels, errors и recovery;
13. offline-ready operation;
14. CLI public commands.

---

## 9. Главный операторский acceptance gate

### 9.1. Критерий одного запроса

Для каждого пользовательского запроса JARVIS должен:

1. понять фактическую цель;
2. соблюдать язык, формат, объём, scope, порядок, запреты и критерии готовности;
3. выполнить доступное действие, если запрошено действие;
4. не заменять доступное исполнение ответом «посмотрите здесь» или «сделайте сами»;
5. задать один точный вопрос только при реальной неоднозначности;
6. выдать сам результат либо создать обещанный artifact и дать точный существующий путь;
7. честно отделить выполненное, частично выполненное и недоступное;
8. не говорить «готово» без фактического результата;
9. не придумывать файлы, IDs, команды, источники, измерения и состояние машины;
10. сохранять ограничения в follow-up;
11. не смешивать разные conversations;
12. выдавать понятный пользовательский ответ.

### 9.2. Неприемлемые результаты

Считай дефектом:

- vague handoff вместо результата;
- необъявленный частичный результат;
- ложное сообщение об успехе;
- лишнее уточнение;
- потерю языка/формата/scope;
- несуществующий path/ID/artifact;
- смешивание conversations;
- duplicate или truncated final;
- traceback вместо понятной ошибки;
- пользовательский ответ, содержащий служебные структуры приложения.

### 9.3. Целостность ответа

Если оператор явно не просил debug/raw формат, обычный чат не должен содержать:

- raw tool-call structures;
- function arguments/envelopes;
- transport chunks/frames;
- internal event objects;
- schema/registry вместо результата;
- role/protocol markers;
- partial или escaped JSON, появившийся из-за parser/streaming ошибки;
- traceback/env dump;
- фрагменты внутренних инструкций.

JSON допустим только при явном запросе JSON и должен быть валидным пользовательским результатом.

Любая непреднамеренная служебная вставка — минимум high-priority finding.

---

## 10. Объём operator suite

На основном интерактивном профиле выполни минимум **40 end-to-end cases**.

Для каждого другого фактически активного профиля выполни минимум **12 representative cases**:

- direct answer;
- strict format;
- multi-turn;
- stream integrity;
- ordinary tool use;
- document/file journey;
- failure/recovery;
- mission/final result;
- отсутствие служебных вставок.

Требования:

- GUI обязателен для всех пользовательских journeys;
- API/CLI проверяются дополнительно, если это отдельный public contract;
- critical cases повторяются минимум 3 раза;
- остальные минимум 2 раза либо имеют обоснованный deterministic single run;
- один FAIL среди повторов означает FAIL/INCONCLUSIVE, а не PASS;
- context-near-limit и long conversation имеют ограниченный budget.

### Минимальный каталог cases

#### Прямые ответы

- короткий вопрос;
- объяснение сложной темы простыми словами;
- сравнение по заданным критериям;
- суммирование предоставленного текста;
- задача без tools: нерелевантные tools не запускаются.

#### Формат

- одно предложение;
- ровно N пунктов;
- Markdown table;
- обычный prose без JSON;
- валидный JSON только по явному запросу;
- сначала результат, затем короткое объяснение;
- русский ответ на mixed-language source;
- заданный filename/path/output type.

#### Multi-turn

- «это», «второй вариант», «тот файл»;
- изменить одно ограничение без потери остальных;
- исправить предыдущий ответ;
- вернуться к исходной цели после длинного follow-up;
- две параллельные conversations;
- новая conversation без временных инструкций старой.

#### Clarification

- однозначная задача без вопроса;
- реально неоднозначная задача с одним точным вопросом;
- продолжение исходной задачи после ответа;
- безопасный очевидный default с явным assumption.

#### Documents/files

На synthetic documents и temp roots:

- загрузить и пересказать;
- найти факт;
- сравнить два документа;
- извлечь таблицу/структуру;
- создать новый artifact;
- изменить только копию;
- преобразовать формат;
- повторно обратиться к ранее загруженному файлу;
- понятная ошибка для повреждённого/неподдерживаемого файла;
- повторная и concurrent генерация не перезаписывает чужой результат;
- неудачная распаковка не оставляет частично опубликованный результат.

#### Runtime и действия

- status/profile/model;
- обычная диагностика;
- read-only запрос не меняет состояние;
- явное безопасное действие выполняется либо корректно запрашивает approval;
- после approval выполняется только указанное действие;
- timeout/cancel/failure не превращается в success;
- текст ответа совпадает с фактическим состоянием.

#### Web/browser

Только обычные разрешённые запросы:

- найти и синтезировать ответ;
- не выдавать голый список ссылок вместо вывода;
- сохранять citations/provenance;
- обозначать cached/stale/live;
- понятно работать при отсутствии интернета;
- browser action применять только когда она нужна;
- итог содержит найденный результат, а не «посмотрите в открытой вкладке».

#### Missions

- создать план по составной задаче;
- выполнить шаги, а не только показать план;
- показывать корректный progress;
- blocked step не завершает mission;
- resume после restart;
- final report отвечает исходной задаче и перечисляет реальные deliverables.

#### Ошибки

- provider unavailable;
- tool unavailable;
- backend unavailable;
- stream interrupted;
- timeout;
- cancel/retry;
- unsupported request;
- permission denied;
- недостаточно данных.

Ошибка должна быть короткой, понятной, правдивой и actionable, без служебных структур и без выдачи partial result за полный.

---

## 11. Технические functional scenarios

### 11.1. Profiles и model mapping

Для каждого активного профиля докажи:

```text
launcher/CLI selection
  -> resolved config
  -> service/container process
  -> provider identity/health
  -> реально загруженная model identity
  -> API/UI display
```

Проверь cold/warm start, repeat start, stop/restart, unknown profile, malformed config, partial stack, occupied port, interrupted startup и cleanup.

### 11.2. Offline-ready

После проверки, что локальные assets существуют:

- запусти без build/pull/download;
- используй обратимый способ временно убрать внешний интернет, сохранив local loopback/dependencies;
- проверь запуск local core;
- проверь понятное поведение явно сетевых функций;
- восстанови соединение и проверь recovery.

Не меняй глобальные network settings, если возврат нельзя доказать.

### 11.3. API/stream/WebSocket

Проверь normal/error/cancel/retry/reconnect, slow consumer, interrupted stream, duplicate/late terminal, cross-session isolation и backend ↔ frontend compatibility.

### 11.4. GUI

Проверь submit/stream/cancel/retry, panels, history restore, status truth, offline/reconnect, long content, resize, keyboard, focus, DPI/zoom, service worker, error messages и отсутствие stale success.

### 11.5. Persistence и recovery

Только на copied/generated state проверь:

- restart во время незавершённого запроса/mission;
- DB lock/read-only/temp unavailability;
- schema/backup/restore/integrity;
- cancel/retry/idempotency;
- concurrent requests/jobs;
- отсутствие duplicate state;
- возврат к normal smoke.

### 11.6. Performance и long run

Измерь по профилям:

- startup/time-to-ready;
- time-to-first-token и completion latency;
- API/UI latency;
- bounded concurrency;
- CPU/RAM/VRAM/disk;
- resource release;
- repeated start/stop/request cycles;
- growth logs/cache/history/temp;
- long conversation/mission;
- bounded soak с watchdog.

Не называй performance observation defect без явной практической непригодности или контракта.

---

## 12. Оценка operator cases

Для каждого case оцени `0/1/2`:

- `intent_fidelity`;
- `task_completion`;
- `constraint_adherence`;
- `truthfulness`;
- `response_integrity`;
- `state_consistency`;
- `recovery_quality`;
- `ux_clarity`.

Где возможно, применяй deterministic validators:

- language/format/count;
- JSON parser/schema;
- expected file/hash/ID/state;
- stream reconstruction;
- duplicate/truncated final detection;
- scan известных служебных markers;
- сравнение заявленного действия с UI/API/storage/tool records.

Семантический результат оцени двумя независимыми review-проходами по одной rubric. Разногласие — `INCONCLUSIVE`.

---

## 13. Findings

Используй категории:

```text
INSTRUCTION_IGNORED
FORMAT_BREACH
WRONG_LANGUAGE
UNNECESSARY_CLARIFICATION
CONTEXT_LOSS
PARTIAL_RESULT_UNDISCLOSED
VAGUE_HANDOFF
FALSE_SUCCESS
CLAIMED_ARTIFACT_MISSING
TOOL_STATE_MISMATCH
INTERNAL_OUTPUT_LEAK
STREAM_FRAGMENT_LEAK
DUPLICATE_FINAL
TRUNCATED_OUTPUT
ERROR_NOT_ACTIONABLE
CROSS_SESSION_MIX
RESULT_NOT_USEFUL
STARTUP_FAILURE
PROFILE_MISMATCH
STATE_RECOVERY_FAILURE
PERFORMANCE_DEGRADATION
```

Каждый finding содержит sanitized user request/response, expected outcome, evidence, repeats, affected profiles/surfaces, root cause/hypothesis и binary acceptance criteria.

Не скрывай воспроизводимый поведенческий дефект под общим «LLM nondeterminism».

---

## 14. PASS gate функциональной готовности

`functional/READY` создаётся только если:

- zero служебных вставок в обычный пользовательский чат;
- zero false-success cases;
- zero cross-session mixing;
- zero missing claimed artifacts среди выполненных cases;
- все P0/P1 journeys имеют PASS либо точный конечный blocker;
- не менее 90% остальных выполненных operator cases имеют PASS;
- каждый FAIL имеет finding и disposition;
- real-model + GUI suite выполнен;
- все active profiles имеют representative coverage;
- startup/profile/model/stream/recovery checks имеют конечный status;
- functional scenario queue не содержит `NOT_RUN`;
- система возвращена в documented state.

Если эти условия не выполнены, marker запрещён.

---

## 15. Подготовка очереди для Spark

Создай только функциональную очередь:

```text
functional/spark/START_HERE.md
functional/spark/QUEUE.csv
functional/spark/PROGRESS.md
functional/spark/TASK_SCHEMA.md
functional/spark/tasks/SPARK-0001.md
```

В неё входят:

- подтверждённые обычными journeys user-facing defects;
- startup/profile/stream/recovery defects, проверенные этой кампанией;
- document partial-output и output-collision defects, если они воспроизведены;
- необходимые test-foundation/validation tasks.

Не импортируй старые findings/queue целиком. Всё, что не воспроизведено этой функциональной кампанией, остаётся в `RESIDUAL_GAPS.md` как `DEFERRED_REVIEW` и не передаётся Spark.

Одна task = одна root cause, небольшой scope, exact harmless reproduction, regression test, allowed files, validation, cleanup и binary acceptance criteria.

Перед созданием `functional/spark/READY` выполни consistency check:

- task имеет source finding;
- finding имеет evidence;
- task имеет reproduction/test/acceptance;
- dependencies acyclic;
- READY task не зависит от blocker;
- нет task, требующей недоказанного behavior;
- marker отсутствует при незавершённом operator gate.

Spark должен запускаться только через:

```text
docs/audit/07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md
```

---

## 16. Завершение

Обнови `FUNCTIONAL_STATE.json` и корневой `PIPELINE_STATE.json`, не стирая историю прежней PHASE B.

Добавь поля:

```json
{
  "phase_b_functional": {
    "status": "COMPLETE | COMPLETE_WITH_BLOCKERS | INCOMPLETE",
    "operator_ready": true,
    "functional_run_path": ".audit/runs/<RUN_ID>/functional"
  },
  "phase_b_extended": {
    "status": "DEFERRED"
  },
  "spark_functional": {
    "status": "READY | PARTIALLY_READY | LOCKED"
  }
}
```

Не создавай `.audit/LATEST_COMPLETE_RUN.txt` для старой расширенной кампании. Создай:

```text
.audit/LATEST_FUNCTIONAL_RUN.txt
```

с единственной строкой:

```text
.audit/runs/<RUN_ID>/functional
```

---

## 17. Финальный ответ

Ответ пользователю должен быть коротким и только на русском:

- готовность 100%;
- functional run path;
- current HEAD/source drift;
- profiles и model mapping;
- operator cases/repeats и PASS/FAIL/BLOCKED/INCONCLUSIVE;
- instruction-following и response-integrity pass rates;
- counts findings и functional Spark tasks;
- paths к основным reports;
- существует ли `functional/READY`;
- существует ли `functional/spark/READY`;
- конечное состояние JARVIS/Docker/LLM;
- список реально оставшихся functional gaps.

Не вставляй raw logs, длинные transcripts или содержимое старых расширенных findings. Если работа остановилась, сохрани artifacts и сообщи только точный blocker и процент.