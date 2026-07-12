# JARVIS — PHASE C: ИСПРАВЛЕНИЕ РЕЗУЛЬТАТОВ АУДИТА ЧЕРЕЗ CODEX SPARK

Ты работаешь как дисциплинированный исполнитель уже завершённого двухкомпонентного аудита JARVIS. Не проводи новый всеобъемлющий аудит и не пытайся держать весь проект и весь отчёт в контексте. Аудиторы PHASE A и PHASE B должны были превратить подтверждённые проблемы в маленькие, упорядоченные и проверяемые задачи.

Твоя цель — последовательно обработать всю доступную очередь `READY`: для каждой задачи сначала воспроизвести исходный дефект, добавить regression test, сделать минимальное исправление, выполнить все проверки, обновить журнал и создать отдельный локальный commit. Работай до тех пор, пока не останется выполнимых `READY`-задач либо пока среда не поставит конкретный блокер.

---

## 0. Каталоги

Рабочий Git-репозиторий находится строго в:

```text
D:\jarvis-gpt
```

Тяжёлые runtime-данные, модели, Docker-данные и большие evidence-файлы находятся в:

```text
D:\jarvis
```

`D:\jarvis` не является репозиторием. Не создавай там `.audit`, не редактируй там исходники и не используй его как working tree.

Начни:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
```

Ожидаемый результат — `D:/jarvis-gpt` с допустимой разницей в регистре и разделителях. Если путь недоступен или Git root иной, остановись и сообщи точный blocker; не подменяй корень каталогом `D:\jarvis`.

---

## 1. Не начинай по незавершённому аудиту

Проверь наличие:

```text
.audit/LATEST_COMPLETE_RUN.txt
```

Если его нет, допустим fallback на `.audit/LATEST_RUN.txt`, но только если указанный run содержит:

```text
PIPELINE_STATE.json
spark/READY
spark/START_HERE_FOR_SPARK.md
spark/SPARK_MASTER_PROMPT.md
spark/SPARK_QUEUE.csv
spark/SPARK_PROGRESS.md
spark/TASK_SCHEMA.md
```

Прочитай `PIPELINE_STATE.json`. Начинать разрешено только если:

- `phase_a.status` равен `COMPLETE` или `COMPLETE_WITH_BLOCKERS`;
- `phase_b.status` равен `COMPLETE` или `COMPLETE_WITH_BLOCKERS`;
- `spark.status` равен `READY` или `PARTIALLY_READY`;
- существует marker `spark/READY`;
- очередь не содержит скрытых обязательных `NOT_RUN` runtime-проверок для выбранной задачи.

Если эти условия не выполнены, не пытайся самостоятельно «доделать аудит» и не чини по сырым гипотезам. Запиши точный blocker в ответ и остановись.

Открой run-каталог и полностью прочитай только:

1. `spark/START_HERE_FOR_SPARK.md`;
2. `spark/SPARK_MASTER_PROMPT.md`;
3. `spark/SPARK_QUEUE.csv`;
4. `spark/SPARK_PROGRESS.md`;
5. `spark/TASK_SCHEMA.md`;
6. repository instructions (`AGENTS.md`, `CODEX.md` и эквиваленты).

Не загружай сразу все findings, evidence и scenario matrices.

---

## 2. Проверка версии кода и пользовательских изменений

Зафиксируй:

```powershell
git status --short --branch
git rev-parse HEAD
git log -1 --oneline
```

Сопоставь текущий commit с baseline/current commit, указанным в audit run и текущей task.

Если production-код изменился после аудита:

- не применяй задачу вслепую;
- вычисли, затронуты ли её `context_files`, `allowed_files`, symbols, contract или tests;
- если drift не затрагивает задачу и reproduction всё ещё даёт ожидаемый FAIL, можно продолжить с записью drift в task report;
- если root cause, reproduction или acceptance больше не соответствуют коду, пометь задачу `BLOCKED_BY_DRIFT` и продолжи следующую независимую READY-задачу;
- не сбрасывай изменения пользователя.

Если working tree содержит несвязанные пользовательские изменения:

- не stage их;
- не форматируй весь репозиторий;
- не используй `git add -A`;
- меняй только разрешённые task-файлы;
- если безопасно отделить изменения невозможно, пометь текущую задачу `BLOCKED_BY_USER_CHANGES`.

Запрещены `git reset --hard`, агрессивный `git clean`, переписывание истории и удаление пользовательских данных.

---

## 3. Выбор следующей задачи

Из `SPARK_QUEUE.csv` выбирай первую по `order` задачу, которая одновременно:

- имеет status `READY`;
- все `depends_on` имеют status `DONE`;
- не конфликтует с незавершённой задачей;
- принадлежит текущему или следующему допустимому batch;
- не требует отсутствующего обязательного ресурса.

Работай **строго по одной задаче**. Для неё прочитай:

- `spark/tasks/SPARK-NNNN.md`;
- только перечисленные `context_files`;
- только связанные source findings;
- только релевантные evidence excerpts/paths;
- repository instructions для затронутых каталогов.

Не читай весь аудит «на всякий случай»: это уменьшает точность и засоряет контекст.

Если очередь пуста, но есть `BLOCKED_BY_SPEC`/`BLOCKED_BY_ENV`/`BLOCKED_BY_DRIFT`, подготовь итоговый список и не импровизируй продуктовые решения.

---

## 4. Обязательный цикл одной задачи

### Шаг 1. Baseline

Запиши в task report:

- UTC timestamp;
- текущий commit;
- branch;
- `git status`;
- профиль/runtime state;
- используемые tool versions;
- связанные finding/scenario IDs.

### Шаг 2. Воспроизведение до изменения

Выполни точные команды `Reproduction before change`.

Задача может перейти к исправлению только если:

- получен ожидаемый FAIL;
- либо task явно имеет type `test`/`investigation` и её собственный oracle выполнен.

Если дефект не воспроизводится:

1. повтори из указанного clean/known state;
2. проверь допустимый source/environment drift;
3. не вноси speculative fix;
4. пометь `BLOCKED_NOT_REPRODUCED` или `OBSOLETE_REFUTED` с evidence;
5. продолжи следующую независимую задачу.

### Шаг 3. Test first

Добавь или обнови минимальный regression test, который:

- падает на исходной реализации;
- проверяет контракт, а не внутреннюю случайность;
- не зависит от точного nondeterministic текста LLM;
- использует fake/mock для orchestration, если реальная модель не нужна;
- не требует внешней сети без явной необходимости;
- воспроизводит bug на соответствующем уровне: unit/integration/E2E/runtime.

Запусти test до production patch и сохрани ожидаемый FAIL. Если task объясняет, почему test-first физически невозможен, следуй указанному альтернативному oracle и зафиксируй это.

### Шаг 4. Минимальный patch

- меняй только `allowed_files`;
- не трогай `forbidden_files`;
- не делай несвязанный refactor, rename, cleanup или dependency upgrade;
- сохрани публичные contracts, кроме явно требуемого изменения;
- не расширяй permissions и не ослабляй validation ради зелёного теста;
- не hardcode test input;
- не скрывай ошибку broad exception handler, sleep или отключением проверки;
- не меняй tests так, чтобы они подтверждали неправильное поведение;
- не правь generated/vendor/runtime data без явного указания task.

Если реальная root cause отличается, но остаётся в documented scope, обнови task и примени минимальное доказуемое исправление. Если требуется другая подсистема, более пяти production-файлов, новая архитектура или product decision — `BLOCKED_SCOPE_ESCALATION`.

### Шаг 5. Проверка

Выполни в таком порядке:

1. новый regression test;
2. узкий suite затронутого компонента;
3. соседний regression suite;
4. все exact validation commands из task;
5. profile/runtime/GUI checks, если требуются;
6. второй профиль и общие пути, если изменён shared code;
7. cleanup и повторный known-good smoke;
8. `git diff --check`;
9. просмотр полного task diff.

Старые audit evidence не заменяют повторную проверку после patch.

Нельзя объявлять DONE, если обязательная validation-команда не запущена. Недоступная команда должна дать `BLOCKED_BY_ENV`, а не воображаемый PASS.

### Шаг 6. Проверка diff

Проверь:

- нет ли секретов, больших логов, моделей, cache/runtime files;
- нет ли форматирования несвязанных файлов;
- нет ли случайного изменения lockfiles;
- не попали ли пользовательские изменения;
- production diff соответствует одному defect;
- regression test действительно ловил исходную ошибку;
- task/audit progress обновлены.

### Шаг 7. Отчёт и статус

В `spark/tasks/SPARK-NNNN.md` добавь фактический execution report:

- status;
- started/finished UTC;
- baseline и drift;
- reproduction commands/results;
- actual root cause;
- changed files/symbols;
- test-first proof;
- validation commands, exit codes, durations;
- profile/GUI/runtime evidence;
- cleanup state;
- remaining risks;
- commit SHA либо blocker.

Обнови `SPARK_PROGRESS.md` и `SPARK_QUEUE.csv`.

Статус `DONE` допустим только при выполнении всех binary acceptance criteria.

### Шаг 8. Один локальный commit

Stage только файлы текущей задачи явными путями. Никогда не используй `git add -A` в грязном дереве.

Создай **один локальный commit** с предложенным сообщением. Не push, не rebase, не squash чужие commits и не открывай PR.

Commit должен включать:

- production fix;
- regression tests;
- task execution report;
- обновление progress/queue, относящееся к задаче.

После commit ещё раз проверь `git status` и сохрани SHA.

---

## 5. Runtime и safety

Задачи могут требовать Docker, WSL, GPU, моделей, GUI и данных под `D:\jarvis`. Следуй task-specific preconditions/cleanup.

Общие запреты:

- не удаляй рабочие volumes/models/user DB;
- не выполняй broad prune;
- corrupt/disk-full/permission tests — только на копиях/тестовых roots;
- не останавливай посторонние workloads;
- не атакуй внешние системы;
- используй synthetic secrets;
- не выполняй опасные model-generated команды на реальном host;
- после каждого runtime test возвращай систему в документированное состояние.

Если task требует опасное действие без безопасной изоляции, пометь `BLOCKED_BY_SAFETY`.

---

## 6. Batches и validation checkpoints

Не переходи в следующий batch, пока:

- все доступные задачи текущего batch имеют конечный status;
- выполнена предусмотренная validation task;
- общие suites зелёные;
- runtime возвращён к known-good state;
- `SPARK_PROGRESS.md` обновлён.

Если batch validation обнаружила регрессию:

1. не переписывай историю завершённых commits;
2. найди минимальный responsible task/commit;
3. если проблема укладывается в его contract, создай следующий свободный `SPARK-NNNN` follow-up task со ссылкой на finding/task/commit;
4. добавь её непосредственно перед повторной validation в queue;
5. исправь тем же обязательным циклом;
6. если root cause/contract неоднозначны — заблокируй batch и не переходи дальше.

Не пропускай validation ради количества DONE-задач.

---

## 7. Новые проблемы, найденные при исправлении

Не расширяй текущую задачу случайно.

Если обнаружен новый независимый defect:

- сохрани минимальное evidence;
- создай новый finding/task только если контракт однозначен, воспроизведение доказано и проблема мешает текущей validation;
- иначе внеси его в `SPARK_PROGRESS.md` как `NEW_FINDING_NEEDS_AUDIT`;
- продолжи текущую задачу, если это безопасно;
- не превращай один patch в ремонт всей подсистемы.

Новый security/data-loss defect, способный сделать дальнейшие действия опасными, блокирует соответствующий workstream до документированного решения.

---

## 8. Устойчивость к длинной работе и compaction

После каждой задачи и каждого batch обновляй `SPARK_PROGRESS.md` так, чтобы новая сессия могла продолжить без старого контекста. В нём держи:

- run ID и baseline commits;
- current branch/HEAD;
- DONE/BLOCKED/READY counts;
- последний завершённый task и commit;
- текущий runtime state;
- следующий eligible task;
- outstanding blockers;
- точную следующую команду.

После compaction или новой сессии сначала перечитай:

```text
PIPELINE_STATE.json
spark/SPARK_QUEUE.csv
spark/SPARK_PROGRESS.md
```

Затем загрузись только в следующую task.

---

## 9. Когда работа считается завершённой

Продолжай автономно, пока не выполнено одно из условий:

1. нет ни одной eligible `READY`-задачи;
2. все READY-задачи DONE и все batch/final validation tasks PASS;
3. оставшиеся задачи имеют только конечные blockers;
4. безопасное продолжение невозможно из-за environment/user changes/runtime risk.

После последнего batch выполни финальную validation task из queue. Не изобретай собственный «полный тест», если аудиторы уже дали точные команды; дополнить их можно, заменить — нет.

Обнови `SPARK_PROGRESS.md` итогом:

- DONE/BLOCKED/OBSOLETE counts;
- commits по task;
- финальные suites и результаты;
- проверенные profiles/runtime/GUI;
- unresolved findings;
- residual risk;
- конечный system state;
- next human action, только если нужен.

Не меняй исходный `ASSURANCE_STATEMENT.md` так, будто исправления автоматически доказали всю систему. Создай `spark/REMEDIATION_SUMMARY.md` и `spark/POST_FIX_VALIDATION.md`, если это предусмотрено master prompt/queue.

---

## 10. Финальный ответ

Не вставляй огромные логи. Сообщи:

- run path;
- branch и final HEAD;
- количество DONE/BLOCKED/OBSOLETE/remaining READY;
- список task → commit;
- какие batch/final validation suites прошли;
- какие profiles/runtime/GUI реально перепроверены;
- оставшиеся blockers и residual risks;
- конечное состояние JARVIS и Docker/LLM;
- путь к `spark/SPARK_PROGRESS.md` и `spark/POST_FIX_VALIDATION.md`.

Не говори «всё исправлено», если остались blockers, неисполненные acceptance checks или непроверенные high-risk области.
