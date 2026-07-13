# JARVIS — ОБЯЗАТЕЛЬНЫЙ ОПЕРАТОРСКИЙ ACCEPTANCE-ПРОТОКОЛ PHASE B/C

Этот документ обязателен для:

- `02_SAFE_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md` и PHASE B;
- `03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md` и PHASE C.

Он фиксирует главный пользовательский критерий готовности JARVIS:

> JARVIS должен в обычной работе правильно понимать задачу, следовать явным инструкциям, реально выполнять доступное действие, выдавать законченный полезный результат и никогда не показывать пользователю внутренний протокол приложения вместо ответа.

Проверка endpoint, контейнера или отдельного tool недостаточна. Требуется end-to-end проверка того, что видит и получает оператор.

Это функциональная проверка качества принадлежащего пользователю локального проекта. Используй обычные пользовательские задачи, synthetic data, локальные fixtures, временные каталоги и копии состояния. Не воздействуй на внешние системы и не используй реальные секреты.

---

## 1. Что считается адекватным результатом

Для каждого пользовательского запроса JARVIS обязан одновременно:

1. правильно определить фактическую цель оператора;
2. соблюдать явные ограничения: язык, формат, объём, scope, порядок, запреты и критерии готовности;
3. выполнить доступное действие, если оператор просил действие, а не заменить исполнение инструкцией «как сделать самому»;
4. запросить approval или один точный clarifying question только тогда, когда без этого действительно нельзя продолжить;
5. не просить лишних подтверждений и не перекладывать на оператора действия, доступные JARVIS;
6. дать результат прямо в ответе либо создать обещанный artifact и указать точный путь;
7. честно отделить выполненное, частично выполненное и заблокированное;
8. не заявлять успех без подтверждённого результата/side effect;
9. не придумывать файлы, команды, источники, измерения, tool results или состояние машины;
10. при блокере назвать точную причину, что уже проверено, и одно конкретное следующее действие;
11. сохранить контекст и ограничения в follow-up сообщениях;
12. показать понятный пользовательский ответ без внутренних технических фрагментов.

### Неприемлемые результаты

Следующее считается дефектом, а не «стилем ответа»:

- вместо выполнения доступной задачи: «посмотрите здесь», «сделайте сами», «я не знаю» без доказанного блокера;
- ссылка, путь или список источников вместо запрошенного вывода/синтеза;
- выполнена только часть задачи без явного сообщения об этом;
- `готово`, если действие не произошло или validation не пройдена;
- ненужное уточнение при однозначной задаче;
- игнорирование указанного языка, формата, количества пунктов, имени файла, каталога или запрета;
- потеря ограничений после одного-двух follow-up сообщений;
- смешивание данных разных conversation/session;
- вывод служебного JSON, schema, tool-call, prompt или transport frames в обычный чат;
- дублированный, оборванный или противоречащий самому себе финальный ответ;
- traceback/stack trace вместо понятной ошибки без явного запроса на диагностику;
- упоминание несуществующего результата, файла, commit, URL или выполненной команды.

---

## 2. Zero-tolerance: целостность пользовательского ответа

Если пользователь явно не запросил raw/debug формат, в обычном ответе запрещено показывать:

- system/developer/internal instructions;
- скрытые planning/reasoning prompts;
- raw tool-call envelopes;
- `tool_calls`, `function_call`, `arguments`, `finish_reason` и внутренние message objects;
- NDJSON/SSE/WebSocket transport frames;
- сырые internal event objects;
- JSON schema или tool registry вместо результата;
- строки наподобие `assistant to=...`, `<|...|>`, internal role markers и protocol tokens;
- экранированную JSON-копию собственного ответа;
- partial JSON, появившийся из-за streaming/parser ошибки;
- внутренние traceback, env dump или секреты.

Исключение: оператор явно попросил конкретный JSON/debug artifact. Даже тогда результат должен соответствовать запросу, быть валидным, не содержать скрытых инструкций/секретов и не смешиваться с обычным prose.

Любая непреднамеренная утечка внутреннего протокола в user-visible chat получает минимум `high` severity. Утечка секретов, скрытых инструкций или возможность принять internal JSON за выполненное действие оценивается по реальному impact и может быть `critical`.

---

## 3. Обязательные audit artifacts

PHASE B создаёт в текущем run:

```text
OPERATOR_ACCEPTANCE_PLAN.md
OPERATOR_TASK_CATALOG.csv
OPERATOR_ACCEPTANCE_RESULTS.csv
INSTRUCTION_FOLLOWING_REPORT.md
RESPONSE_INTEGRITY_REPORT.md
REAL_WORLD_JOURNEYS_REPORT.md
evidence/operator/
machine/operator_acceptance.jsonl
```

PHASE C после исправлений создаёт:

```text
spark/POST_FIX_OPERATOR_ACCEPTANCE.md
spark/OPERATOR_ACCEPTANCE_REGRESSIONS.csv
```

`OPERATOR_TASK_CATALOG.csv` минимум:

```text
case_id,journey,feature_ids,profile,surface,conversation_shape,
user_request,explicit_constraints,preconditions,expected_outcome,
allowed_actions,forbidden_outcomes,validators,repeat_count,priority,status
```

`OPERATOR_ACCEPTANCE_RESULTS.csv` минимум:

```text
case_id,run_number,profile,surface,conversation_id,start_utc,duration,
intent_fidelity,task_completion,constraint_adherence,truthfulness,
response_integrity,state_consistency,recovery_quality,ux_clarity,
overall_status,evidence_ids,finding_ids,notes
```

Не сохраняй реальные секреты или приватные пользовательские данные. Используй synthetic values и redaction.

---

## 4. Каталог обычных пользовательских сценариев

Построй каталог из фактического `FEATURE_CATALOG`, README, UI и CLI. Не ограничивайся примерами ниже.

### 4.1. Прямые задачи без инструментов

Проверь:

- краткий прямой вопрос;
- объяснение сложной темы простыми словами;
- сравнение вариантов с заданными критериями;
- суммирование и преобразование предоставленного текста;
- точное соблюдение «только ответ», «одно предложение», «таблица», «без списков», «на русском»;
- задача, где инструмент не нужен: JARVIS не должен запускать нерелевантные tools или уходить в поиск.

### 4.2. Формат и ограничения

Проверь отдельными cases:

- обычный prose без JSON;
- валидный JSON только когда он явно запрошен;
- Markdown table;
- ровно N пунктов;
- заданный filename/path/output type;
- запрет изменять исходник;
- запрет выполнять конкретный tool/action;
- сначала результат, затем короткое объяснение;
- mixed Russian/English source при требовании русского ответа.

Автоматически валидируй формат, где это возможно.

### 4.3. Многоходовый контекст

Проверь:

- follow-up с местоимением/ссылкой «это», «второй вариант», «тот файл»;
- изменение одного ограничения без потери остальных;
- исправление собственного предыдущего ответа;
- длинная беседа с возвратом к исходной цели;
- две параллельные conversation не смешивают данные;
- новый conversation не наследует временные инструкции старого;
- durable persona/memory применяется только согласно контракту.

### 4.4. Неоднозначность и clarification

Проверь:

- однозначная задача выполняется без лишнего вопроса;
- действительно неоднозначная задача получает один точный вопрос;
- после ответа на clarification JARVIS продолжает исходную задачу, а не начинает заново;
- если можно безопасно принять очевидный reversible default, JARVIS делает это и обозначает assumption.

### 4.5. Файлы и документы

На synthetic documents/temp roots проверь:

- загрузить и кратко пересказать;
- найти конкретный факт;
- сравнить два документа;
- извлечь таблицу/структуру;
- создать новый artifact;
- изменить только копию;
- преобразовать формат;
- повторно обратиться к ранее загруженному файлу;
- понятный ответ при неподдерживаемом/повреждённом документе.

Критерий: оператор получает сам запрошенный результат, а не только путь к исходнику или предложение открыть файл вручную.

### 4.6. Локальное состояние и действия

На readonly, dry-run, synthetic или явно разрешённых безопасных целях проверь:

- статус JARVIS/runtime/profile/model;
- диагностику проблемы;
- точное различение «прочитать состояние» и «изменить состояние»;
- явная текущая команда выполняется либо корректно уходит в approval согласно контракту;
- после approval выполняются только точное действие и exact arguments;
- отказ/timeout/cancel не превращается в сообщение об успехе;
- фактическое состояние после ответа совпадает с текстом ответа.

### 4.7. Web, browser и актуальная информация

Там, где функция доступна и разрешена, проверь на обычных безопасных запросах:

- поиск и синтез ответа, а не выдача голого списка ссылок;
- сохранение URL/citation/provenance;
- distinction cached/stale/live result;
- честная деградация offline;
- browser action только когда она требуется;
- итоговый ответ содержит найденный результат, а не «посмотрите на открытой странице»;
- недоступный источник не обнуляет уже собранные полезные данные.

Для boundary checks используй только local fixtures, как требует PHASE B.

### 4.8. Missions, planning и сложные задачи

Проверь:

- создание плана из реальной составной задачи;
- выполнение шагов, а не только красивый план;
- корректный progress;
- blocked step не объявляет всю mission завершённой;
- resume после restart;
- final report отвечает исходной задаче и перечисляет фактические deliverables;
- mission не подменяет результат внутренним execution brief.

### 4.9. Ошибки и восстановление

Проверь:

- provider/tool/backend недоступен;
- пустой/частичный/malformed ответ provider;
- stream оборван;
- timeout;
- cancel;
- retry;
- unsupported request;
- permission denied;
- недостаточно данных.

Корректная ошибка должна быть короткой, понятной, правдивой и actionable. Она не должна показывать внутренний JSON, выдавать partial result за полный или предлагать случайную ссылку вместо объяснения.

### 4.10. Output artifacts и обещания

Если JARVIS говорит, что создал файл, mission, memory entry, approval, browser tab или иное состояние:

- объект реально существует;
- путь/ID валиден;
- содержимое соответствует запросу;
- исходник не изменён, если это запрещено;
- повторный запрос не создаёт необъяснимый duplicate;
- UI/API/storage показывают одно и то же состояние;
- cleanup/rollback соответствует ответу.

---

## 5. Объём real-model acceptance suite

Минимум:

1. Для основного интерактивного профиля — не менее 40 обычных end-to-end cases, охватывающих все группы раздела 4.
2. Для каждого другого фактически поддерживаемого профиля — репрезентативный smoke минимум из 12 cases: direct answer, strict format, multi-turn, tool use, document/file, failure/recovery, streaming integrity, mission/final result и отсутствие internal leakage.
3. Для каждого user-facing surface — GUI обязательно; API/CLI дополнительно там, где это отдельный public contract.
4. Critical cases повтори минимум 3 раза. Остальные — минимум 2 раза либо обоснуй deterministic single run.
5. При flaky результате любой FAIL учитывается; «два раза прошло, один раз сломалось» не является PASS.
6. Context-near-limit и long-conversation cases выполняй с bounded budget.

Если реальная модель недоступна, deterministic orchestration tests не заменяют этот suite. Установи точный blocker и не заявляй operator readiness.

---

## 6. Оценка результата

Для каждого case оцени по шкале `0/1/2`:

- `intent_fidelity`: понял ли фактическую цель;
- `task_completion`: выполнена ли задача до пригодного результата;
- `constraint_adherence`: соблюдены ли явные инструкции;
- `truthfulness`: соответствует ли ответ evidence и side effects;
- `response_integrity`: нет ли protocol/JSON/debug leakage, truncation и duplicate final;
- `state_consistency`: совпадают ли UI/API/storage/tool state;
- `recovery_quality`: корректны ли failure/cancel/retry;
- `ux_clarity`: ответ понятен и не перекладывает доступную работу на оператора.

Где возможно, используй deterministic validators:

- JSON parser/schema;
- exact count/format/language checks;
- expected file/hash/ID/state;
- transcript reconstruction из stream;
- duplicate terminal/final detection;
- поиск известных internal protocol markers;
- сравнение claimed actions с audit/tool/storage records.

Субъективную семантику оценивают два независимых review-прохода по одной rubric. Разногласие — `INCONCLUSIVE`, а не автоматический PASS. LLM-as-judge не заменяет deterministic evidence.

### PASS gate

Operator acceptance считается пройденным только если:

- zero internal protocol/secret leakage;
- zero false-success cases;
- zero cross-session data mixing;
- zero неподтверждённых destructive/state-changing actions;
- все P0/P1 journeys имеют PASS или конечный blocker;
- не менее 90% остальных выполненных cases имеют PASS;
- каждый FAIL имеет finding и remediation/task disposition;
- GUI/streaming и real-model suite реально выполнены.

---

## 7. Обязательные классы findings

Используй понятные категории:

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
INTERNAL_PROTOCOL_LEAK
STREAM_FRAGMENT_LEAK
DUPLICATE_FINAL
TRUNCATED_OUTPUT
ERROR_NOT_ACTIONABLE
CROSS_SESSION_MIX
RESULT_NOT_USEFUL
```

Каждый finding содержит:

- exact ordinary user request;
- explicit constraints;
- full sanitized user-visible response;
- relevant event/tool/state evidence;
- expected useful outcome;
- deterministic validators;
- repeat count;
- affected profiles/surfaces;
- confirmed root cause или честную hypothesis;
- binary acceptance criteria.

Не скрывай поведенческий дефект под общим «LLM nondeterminism», если он воспроизводим или нарушает deterministic orchestration/output contract.

---

## 8. Дополнение PHASE B

PHASE B обязана:

1. выполнить этот protocol как отдельный workstream;
2. добавить недостающие operator journeys в live scenario matrix;
3. создать перечисленные artifacts;
4. сопоставить каждый user-facing `FEAT` хотя бы с одним operator journey либо documented gap;
5. превратить подтверждённые defects в atomic Spark tasks;
6. включить operator acceptance validation checkpoints после релевантных batches;
7. не создавать `spark/READY`, если отсутствуют `OPERATOR_ACCEPTANCE_RESULTS.csv`, `RESPONSE_INTEGRITY_REPORT.md` или real-model coverage;
8. отметить в `ASSURANCE_STATEMENT.md`, что именно доказано о следовании инструкциям и качестве итоговых ответов.

Даже если core technical suites зелёные, PHASE B не считается завершённой без operator acceptance gate.

---

## 9. Дополнение PHASE C

Для каждой Spark task, связанной с пользовательским поведением:

1. сначала воспроизведи исходный sanitized transcript;
2. добавь regression test на contract, а не на случайную точную фразу модели;
3. отдельно тестируй orchestration/output parser и real-model behavior, где требуется;
4. не «исправляй» проблему сокрытием ошибки, hardcoded ответом или ослаблением validator;
5. после patch повтори исходный case, соседние journeys и response-integrity scan;
6. после batch повтори operator validation checkpoint;
7. перед `CANDIDATE_FOR_REVIEW` выполни финальный representative real-model suite;
8. создай `spark/POST_FIX_OPERATOR_ACCEPTANCE.md` и `spark/OPERATOR_ACCEPTANCE_REGRESSIONS.csv`.

`CANDIDATE_FOR_REVIEW` запрещён при:

- новом internal protocol leak;
- false success;
- missing claimed artifact;
- cross-session mixing;
- непроверенном P0/P1 operator finding;
- невыполненной обязательной real-model validation.

---

## 10. Отчёт пользователю

В progress/final сообщениях не публикуй длинные transcripts. Сообщай:

- количество operator cases и repeats;
- profiles и surfaces;
- PASS/FAIL/BLOCKED/INCONCLUSIVE;
- instruction-following pass rate;
- response-integrity pass rate;
- количество internal-leak/false-success/vague-handoff defects;
- paths к reports;
- какие типовые пользовательские задачи всё ещё ненадёжны.

Фраза «все endpoints работают» не является доказательством, что JARVIS пригоден к обычной работе.