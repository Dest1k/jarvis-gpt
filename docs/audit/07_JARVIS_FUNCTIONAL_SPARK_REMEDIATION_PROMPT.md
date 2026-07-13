# JARVIS — ФУНКЦИОНАЛЬНЫЕ ИСПРАВЛЕНИЯ ЧЕРЕЗ CODEX SPARK

Исправляй только подтверждённые дефекты из завершённой функциональной проверки JARVIS. Не проводи новый полный аудит и не загружай старую расширенную очередь.

Главный критерий: после исправлений JARVIS должен правильно понимать обычные задачи, соблюдать явные ограничения, выполнять доступные действия, выдавать законченный полезный результат и не показывать в обычном чате служебные структуры приложения.

Все сообщения пользователю пиши по-русски. Подробности сохраняй в task reports. Отчёты о ходе работы — на 50%, 90% и после каждого batch.

## 1. Входные данные

Начни в `D:\jarvis-gpt` и проверь Git root, branch, HEAD и status.

Прочитай:

```text
docs/audit/08_JARVIS_FUNCTIONAL_REMEDIATION_ROLLBACK_PROTOCOL.md
.audit/LATEST_FUNCTIONAL_RUN.txt
<functional-run>/FUNCTIONAL_STATE.json
<functional-run>/FUNCTIONAL_ASSURANCE_STATEMENT.md
<functional-run>/FUNCTIONAL_FINDINGS_INDEX.md
<functional-run>/OPERATOR_ACCEPTANCE_RESULTS.csv
<functional-run>/INSTRUCTION_FOLLOWING_REPORT.md
<functional-run>/RESPONSE_INTEGRITY_REPORT.md
<functional-run>/spark/START_HERE.md
<functional-run>/spark/QUEUE.csv
<functional-run>/spark/PROGRESS.md
<functional-run>/spark/TASK_SCHEMA.md
repository instructions
```

Не читай старые полные scenario/finding очереди.

Начинать разрешено только при наличии:

```text
<functional-run>/READY
<functional-run>/spark/READY
```

## 2. Изоляция

Следуй `08_JARVIS_FUNCTIONAL_REMEDIATION_ROLLBACK_PROTOCOL.md`.

Все изменения выполняй только в отдельном worktree:

```text
D:\jarvis-gpt-worktrees\functional-<RUN_ID>
```

на ветке:

```text
spark-functional/<RUN_ID>
```

Основной checkout не изменяй. Не выполняй push или merge.

## 3. Работа по очереди

Выбирай по одной задаче со статусом `READY`, выполненными dependencies и доступными preconditions.

Для текущей задачи читай только task file, перечисленные context files, связанный functional finding и нужные sanitized evidence.

Обязательный цикл:

1. Проверь root, branch, HEAD и status.
2. Создай task checkpoint по rollback protocol.
3. Воспроизведи исходный FAIL до изменения.
4. Добавь regression test, который проверяет контракт, а не случайную точную фразу модели.
5. Сделай минимальный patch только в allowed files.
6. Запусти новый test, узкий suite, соседний suite и exact validation commands.
7. Повтори исходный обычный пользовательский сценарий и соседние сценарии.
8. Проверь stream/final output, фактические artifacts и состояние приложения.
9. Выполни cleanup и normal smoke.
10. Проверь полный diff.
11. Обнови task report, queue и progress.
12. Создай один локальный commit.

Если исходный FAIL не воспроизводится, не делай предположительное исправление. Установи точный blocker и переходи к следующей независимой задаче.

Если требуется новая архитектура, другая подсистема или решение пользователя, останови задачу и не расширяй scope самостоятельно.

## 4. Пользовательское поведение

Для задач, затрагивающих chat, GUI, routing, tools, documents, missions, memory или final response:

- сначала сохрани исходный обезличенный user journey;
- зафиксируй язык, формат, scope, ограничения и ожидаемый результат;
- после patch повтори journey требуемое число раз;
- проверь, что обещанный файл/ID/действие реально существует;
- проверь отсутствие ложного сообщения об успехе;
- проверь отсутствие дублированного или оборванного final;
- проверь отсутствие raw debug/tool/transport fragments в обычном ответе;
- проверь multi-turn context и failure/recovery;
- сохрани результаты в `functional/spark/OPERATOR_ACCEPTANCE_REGRESSIONS.csv`.

Unit test не заменяет real-model/GUI recheck, если дефект проявлялся через real model или GUI.

## 5. Batch validation

Перед следующим batch:

- все задачи текущего batch имеют конечный status;
- validation checkpoint PASS;
- JARVIS возвращён в documented state;
- затронутые user journeys повторены;
- новые регрессии не обнаружены;
- progress и rollback state обновлены.

Плохой commit не переписывай: создай отдельный revert commit либо узкую follow-up task.

## 6. Финальная проверка

После последнего batch выполни representative real-model suite:

- основной профиль — минимум 20 ключевых ordinary journeys;
- каждый другой активный профиль — минимум 8 representative journeys;
- critical cases повторить минимум 3 раза;
- GUI, streaming, documents, tool use, mission, multi-turn и failure/recovery обязательны.

Финальный результат допустим только если:

- нет служебных вставок в обычном чате;
- нет ложных сообщений об успехе;
- нет смешивания conversations;
- все обещанные artifacts существуют;
- важные operator tasks имеют PASS или точный blocker;
- startup/profile/model/stream/recovery smoke PASS;
- rollback assets остаются валидными.

Создай:

```text
functional/spark/REMEDIATION_SUMMARY.md
functional/spark/POST_FIX_VALIDATION.md
functional/spark/POST_FIX_OPERATOR_ACCEPTANCE.md
functional/spark/OPERATOR_ACCEPTANCE_REGRESSIONS.csv
functional/spark/ROLLBACK_INDEX.md
```

Финальное состояние:

```text
FUNCTIONAL_CANDIDATE_FOR_REVIEW
```

Не merge и не push.

В финальном ответе кратко укажи run path, worktree/branch/final HEAD, task counts, task-to-commit mapping, validation results, operator pass rates, rollback status, остаточные blockers и конечное состояние JARVIS.