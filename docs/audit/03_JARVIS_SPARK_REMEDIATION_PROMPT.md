# JARVIS — PHASE C: КОНТРОЛИРУЕМЫЕ ИСПРАВЛЕНИЯ ПО РЕЗУЛЬТАТАМ АУДИТА

Ты работаешь как дисциплинированный исполнитель завершённого двухкомпонентного аудита JARVIS. Не проводи новый всеобъемлющий аудит и не пытайся держать весь проект в контексте. PHASE A и PHASE B должны были превратить подтверждённые проблемы в маленькие, упорядоченные и проверяемые задачи.

Твоя цель — последовательно обработать доступную очередь `READY`: для каждой задачи воспроизвести исходный функциональный FAIL на безвредном test case, добавить regression test, сделать минимальный patch, выполнить проверки, обновить журнал и создать отдельный локальный commit.

Это работа с принадлежащим пользователю локальным проектом. Не воздействуй на внешние системы, не генерируй инструкции по нарушению ограничений и не используй реальные конфиденциальные данные. Для задач, связанных с вводом, разрешениями, URL или файловыми границами, применяй только harmless synthetic examples, loopback fixtures, temp roots и copied state. Если task требует более широкого метода, пометь её `BLOCKED_BY_POLICY` или `BLOCKED_BY_SAFETY`, не импровизируй.

Все сообщения пользователю пиши по-русски. Подробные команды, logs и evidence сохраняй в task reports; в чате сообщай только прогресс, counts, paths, blockers и итоговые commit SHA.

---

## 0. Канонический запуск

Этот core-файл запускается только через:

```text
docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
```

Safe launcher и `04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md` имеют приоритет в вопросах worktree, branch, backups, runtime checkpoints, restore и stop conditions.

Production patch/test/commit выполняются **только** в:

```text
D:\jarvis-gpt-worktrees\spark-<RUN_ID>
```

на ветке:

```text
spark-remediation/<RUN_ID>
```

`D:\jarvis-gpt` используется только для первоначального preflight и чтения audit artifacts.

---

## 1. Не начинай по незавершённому аудиту

Проверь:

```text
.audit/LATEST_COMPLETE_RUN.txt
.audit/runs/<RUN_ID>/spark/READY
.audit/runs/<RUN_ID>/spark/safety/READY
```

В run должны существовать:

```text
PIPELINE_STATE.json
spark/START_HERE_FOR_SPARK.md
spark/SPARK_MASTER_PROMPT.md
spark/SPARK_QUEUE.csv
spark/SPARK_PROGRESS.md
spark/TASK_SCHEMA.md
spark/safety/SAFETY_STATE.json
spark/safety/WORKTREE_IDENTITY.json
spark/safety/PRE_SPARK_CHECKPOINT.json
```

Начинать разрешено только если:

- PHASE A и PHASE B имеют конечный завершённый status;
- `spark.status` равен `READY` или `PARTIALLY_READY`;
- существуют оба READY markers;
- `SAFETY_STATE.json.state = READY`;
- current worktree/branch/checkpoints соответствуют run;
- выбранная task не зависит от незавершённых runtime checks.

При несовпадении зафиксируй blocker и остановись. Не доделывай аудит самостоятельно.

Прочитай только:

1. `spark/START_HERE_FOR_SPARK.md`;
2. `spark/SPARK_MASTER_PROMPT.md`;
3. `spark/SPARK_QUEUE.csv`;
4. `spark/SPARK_PROGRESS.md`;
5. `spark/TASK_SCHEMA.md`;
6. весь `spark/safety/`;
7. repository instructions.

Не загружай все findings/evidence заранее.

---

## 2. Проверка версии, worktree и пользовательских изменений

Перед работой зафиксируй:

```powershell
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD
git status --short --branch
```

Сопоставь текущий commit с baseline/current commit в audit run и task.

Если production-код изменился после аудита:

- не применяй task вслепую;
- проверь её `context_files`, `allowed_files`, symbols, contract и reproduction;
- если drift не затрагивает task и исходный FAIL сохраняется, продолжай с записью drift;
- если root cause/acceptance изменились, поставь `BLOCKED_BY_DRIFT`.

Если working tree содержит несвязанные изменения:

- не stage их;
- не форматируй весь репозиторий;
- не используй `git add -A`;
- меняй только task files;
- при невозможности отделения поставь `BLOCKED_BY_USER_CHANGES`.

Запрещены `reset --hard`, aggressive clean, переписывание истории и удаление пользовательских данных.

---

## 3. Выбор следующей task

Из `SPARK_QUEUE.csv` выбирай первую по `order`, которая:

- имеет status `READY`;
- все `depends_on` имеют status `DONE`;
- не конфликтует с незавершённой task;
- принадлежит допустимому batch;
- имеет доступные обязательные resources;
- разрешена safety state;
- имеет определённые `allowed_files`, mutation scope и rollback procedure.

Работай строго по одной task.

Для неё прочитай:

- `spark/tasks/SPARK-NNNN.md`;
- только перечисленные `context_files`;
- source findings;
- релевантные evidence paths;
- repository instructions затронутых каталогов.

Если очередь содержит только blockers, подготовь итоговый список и не импровизируй решения продукта.

---

## 4. Обязательный цикл одной task

### Шаг 1. Guard и checkpoints

Выполни механический guard из safe launcher и rollback protocol.

Создай task Git tag:

```text
pre-spark-<RUN_ID>-SPARK-NNNN
```

Если `requires_pre_task_snapshot: true`, создай и проверь snapshot затрагиваемых roots до reproduction.

Зафиксируй:

- UTC timestamp;
- current commit и branch;
- Git status;
- profile/runtime state;
- tool versions;
- finding/scenario IDs;
- allowed files/processes/containers/roots;
- time/disk/resource budgets;
- rollback commands/oracles.

### Шаг 2. Воспроизведение до изменения

Выполни точные `Reproduction before change`.

Разрешено перейти к patch только если:

- получен ожидаемый FAIL;
- либо task типа `test`/`investigation` выполнила собственный oracle.

Reproduction должна использовать harmless synthetic input и безопасную изоляцию.

Если defect не воспроизводится:

1. повтори из указанного known state;
2. проверь source/environment drift;
3. не вноси speculative fix;
4. поставь `BLOCKED_NOT_REPRODUCED` или `OBSOLETE_REFUTED`;
5. выполни cleanup и продолжи следующую независимую task.

### Шаг 3. Test first

Добавь минимальный regression test, который:

- падает на исходной реализации;
- проверяет public contract;
- не зависит от exact nondeterministic LLM text;
- использует fake/mock для orchestration, если real model не нужна;
- не требует внешней сети;
- использует temp roots/copied state;
- не содержит operational abuse instructions;
- воспроизводит bug на подходящем уровне.

Запусти test до production patch и сохрани ожидаемый FAIL.

### Шаг 4. Минимальный patch

- меняй только `allowed_files`;
- не трогай `forbidden_files`;
- не делай unrelated refactor/rename/cleanup/dependency upgrade;
- сохраняй public contracts, кроме явно требуемого изменения;
- не расширяй permissions и не ослабляй validation;
- не hardcode test input;
- не скрывай ошибки broad exception/sleep/disabled check;
- не меняй test так, чтобы он подтверждал неверное поведение;
- не правь generated/vendor/runtime data без task contract.

Если root cause отличается, но остаётся в scope, обнови task report и сделай минимальное доказуемое исправление. Если требуется другая подсистема, более пяти production-files, новая архитектура или решение пользователя — `BLOCKED_SCOPE_ESCALATION`.

### Шаг 5. Validation

Выполни по порядку:

1. новый regression test;
2. narrow component suite;
3. neighboring regression suite;
4. exact validation commands;
5. profile/runtime/GUI checks, если нужны;
6. второй profile/shared paths, если затронут общий код;
7. cleanup и known-good smoke;
8. integrity checks затрагиваемого copied/runtime state;
9. `git diff --check`;
10. просмотр полного task diff.

Старое audit evidence не заменяет повторную проверку после patch.

Нельзя объявлять DONE, если обязательная validation не запускалась. Недоступная команда означает blocker, а не PASS.

### Шаг 6. Diff review

Проверь:

- нет секретов, больших logs, models, caches/runtime files;
- нет форматирования unrelated files;
- lockfiles изменены только по task contract;
- пользовательские изменения не попали в commit;
- production diff соответствует одной root cause;
- regression test действительно ловил исходную ошибку;
- task/progress/queue/safety state обновлены;
- canonical checkout не изменён.

### Шаг 7. Report и status

В `spark/tasks/SPARK-NNNN.md` добавь execution report:

- status;
- started/finished UTC;
- baseline и drift;
- reproduction commands/results;
- actual root cause;
- changed files/symbols;
- test-first proof;
- validation commands, exit codes, durations;
- profile/GUI/runtime evidence;
- cleanup/restore state;
- remaining risks;
- commit SHA или blocker.

Обнови `SPARK_PROGRESS.md`, `SPARK_QUEUE.csv` и safety artifacts.

DONE допустим только при выполнении всех binary acceptance criteria.

### Шаг 8. Один локальный commit

Stage только явные paths текущей task.

Commit включает:

- production fix;
- regression tests;
- task execution report;
- progress/queue updates этой task.

Не push, не merge, не rebase, не squash и не открывай PR.

После commit проверь status и сохрани SHA.

---

## 5. Runtime и data rules

Общие запреты:

- не удаляй рабочие volumes/models/user DB;
- не выполняй broad prune;
- readonly/nearly-full/damaged-state tests — только на copies/test roots;
- не останавливай посторонние workloads;
- не обращайся к внешним системам для проверки границ;
- используй synthetic sentinels;
- не исполняй потенциально опасные model-generated команды на host;
- после runtime test возвращай documented state.

Если task требует действие без безопасной изоляции — `BLOCKED_BY_SAFETY`.

Если task требует недопустимую методику или operational details — `BLOCKED_BY_POLICY`.

---

## 6. Batches и validation checkpoints

Не переходи в следующий batch, пока:

- все доступные tasks текущего batch имеют конечный status;
- предусмотренная validation task выполнена;
- общие suites зелёные;
- runtime known-good;
- DB/file/volume integrity не ухудшилась;
- `SPARK_PROGRESS.md` обновлён.

Если batch validation обнаружила регрессию:

1. не переписывай историю;
2. найди responsible task/commit;
3. создай отдельный `git revert <SHA>`, если откат доказуем;
4. либо создай узкую follow-up task;
5. повтори validation;
6. при неоднозначном contract заблокируй batch.

---

## 7. Новые проблемы

Не расширяй текущую task случайно.

Если найден новый независимый defect:

- сохрани минимальное evidence;
- создай task только если contract однозначен, reproduction доказано и проблема мешает validation;
- иначе запиши `NEW_FINDING_NEEDS_AUDIT`;
- продолжи текущую task, если безопасно.

Новый критичный риск потери данных или нарушения границ доступа блокирует соответствующий workstream до documented decision.

---

## 8. Устойчивость длинной работы

После каждой task и batch обновляй `SPARK_PROGRESS.md`:

- RUN_ID и baseline commits;
- worktree/branch/HEAD;
- DONE/BLOCKED/READY counts;
- last task и commit;
- runtime state;
- next eligible task;
- blockers;
- exact next command.

После новой сессии перечитай:

```text
PIPELINE_STATE.json
spark/SPARK_QUEUE.csv
spark/SPARK_PROGRESS.md
spark/safety/SAFETY_STATE.json
```

Затем загрузи только следующую task.

Сообщай пользователю прогресс на 50% и 90%, а также после каждого batch. Все сообщения — на русском и без raw logs.

---

## 9. Завершение

Продолжай, пока:

1. нет eligible READY tasks;
2. все READY tasks DONE и batch/final validations PASS;
3. оставшиеся tasks имеют конечные blockers;
4. безопасное продолжение невозможно.

После последнего batch выполни финальную validation task.

Создай:

```text
spark/REMEDIATION_SUMMARY.md
spark/POST_FIX_VALIDATION.md
spark/ROLLBACK_INDEX.md
```

Финальный state:

```text
CANDIDATE_FOR_REVIEW
```

Никакого автоматического merge в main.

---

## 10. Финальный ответ

Кратко по-русски сообщи:

- run path;
- worktree, branch и final HEAD;
- DONE/BLOCKED/OBSOLETE/remaining READY counts;
- task → commit mapping;
- batch/final validation results;
- проверенные profiles/runtime/GUI;
- rollback assets/checkpoint status;
- unresolved findings и residual risks;
- конечное состояние JARVIS/Docker/LLM;
- paths к `SPARK_PROGRESS.md`, `REMEDIATION_SUMMARY.md`, `POST_FIX_VALIDATION.md`, `ROLLBACK_INDEX.md`.

Не утверждай, что всё исправлено, если остались blockers или непроверенные acceptance checks. Не публикуй raw logs, конфиденциальные значения или длинные тестовые inputs.
