# JARVIS — ПРОТОКОЛ ИЗОЛЯЦИИ И ОТКАТА ФУНКЦИОНАЛЬНЫХ ИСПРАВЛЕНИЙ

Этот документ используется только вместе с:

- `06_JARVIS_FUNCTIONAL_RUNTIME_ACCEPTANCE_PROMPT.md`;
- `07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md`.

Его цель — оставить основной checkout и пользовательские данные нетронутыми, пока Spark готовит локальную ветку-кандидат.

## 1. Каталоги

```text
D:\jarvis-gpt
  основной checkout; здесь Spark ничего не исправляет

D:\jarvis-gpt-worktrees\functional-<RUN_ID>
  отдельный worktree для исправлений

D:\jarvis
  модели, runtime-данные и пользовательское состояние

D:\jarvis\audit-backups\<RUN_ID>\functional\
  Git bundle, копии затрагиваемого состояния и manifests
```

`D:\jarvis` не является Git-репозиторием.

## 2. Обязательная Git-изоляция

До первого исправления:

1. Проверь `git status` в `D:\jarvis-gpt`.
2. Создай отдельную ветку:

```text
spark-functional/<RUN_ID>
```

3. Создай отдельный worktree:

```text
D:\jarvis-gpt-worktrees\functional-<RUN_ID>
```

4. Зафиксируй base SHA и pre-Spark SHA.
5. Создай annotated tags:

```text
pre-functional-source-<RUN_ID>
pre-functional-spark-<RUN_ID>
```

6. Создай и проверь Git bundle:

```text
D:\jarvis\audit-backups\<RUN_ID>\functional\git\jarvis-gpt-pre-functional-spark.bundle
```

7. Выполни `git bundle verify` и сохрани SHA-256.

Запрещены push, merge, rebase, squash, force-update, `git reset --hard`, aggressive clean и присвоение пользовательских изменений.

## 3. Основной checkout

В `D:\jarvis-gpt` разрешено только:

- читать audit artifacts и Git metadata;
- проверять status;
- создавать branch/tag/worktree/bundle;
- копировать только текущие audit artifacts в отдельный worktree.

Production/test/config files изменяются только в функциональном worktree.

## 4. Состояние JARVIS

Перед задачей определи, меняет ли она:

```text
code_only
runtime_read_only
runtime_temporary
persistent_state
container_configuration
```

Для `persistent_state` и `container_configuration` обязательно:

- перечислить затрагиваемые paths/volumes;
- остановить только относящиеся к JARVIS writers, если это требуется для консистентной копии;
- создать копию только затрагиваемого ценного состояния;
- проверить файлы, counts и доступность копии;
- для SQLite использовать backup API либо доказанно остановленные writers;
- выполнить `PRAGMA integrity_check` на копии;
- проверить пробное восстановление в отдельный временный каталог;
- сохранить exact restore steps и observable checks.

Модели, images и пересоздаваемые caches не копируются без необходимости.

Если копию или пробное восстановление проверить нельзя, соответствующая задача получает `BLOCKED_BY_SAFETY`. Code-only задачи могут продолжаться при исправных Git-страховках.

## 5. Checkpoint одной задачи

Перед каждой задачей:

1. Проверь Git root, branch, HEAD и status.
2. Создай tag:

```text
pre-functional-<RUN_ID>-SPARK-NNNN
```

3. Зафиксируй allowed files и запрещённые файлы.
4. Если задача меняет состояние, создай task-level copy затрагиваемых objects и проверь restore checks.
5. Зафиксируй начальное состояние JARVIS, profile, containers, ports и health, когда это относится к задаче.

## 6. Если задача не удалась

До commit:

1. Останови только процессы, созданные этой задачей.
2. Сохрани task report.
3. Восстанови только explicit allowed files из task tag.
4. Не используй broad reset/clean.
5. При изменении runtime восстанови task-level copy.
6. Проверь исходный smoke и integrity checks.
7. Оставь задачу с точным blocker.

После commit, если batch validation обнаружила регрессию:

- определи responsible commit;
- создай отдельный `git revert <SHA>`, если откат проверяем;
- либо создай узкую follow-up task;
- повтори regression и обычный пользовательский smoke.

## 7. Немедленная остановка

Остановись без новых изменений, если:

- root/branch/worktree не совпадают с manifest;
- основной checkout получил изменения от Spark;
- появился необъяснимый dirty diff;
- задача выходит за allowed files или declared runtime scope;
- резервная копия или пробное восстановление не проходят;
- SQLite integrity check не PASS;
- затронут посторонний process/container/volume;
- свободное место ниже заранее рассчитанного минимума;
- JARVIS не возвращается в документированное состояние;
- невозможно точно определить изменённые данные.

Не удаляй worktree, branch, tags, bundle, backups или evidence после остановки.

## 8. Commit policy

Одна успешная задача = один локальный commit, включающий:

- минимальное исправление;
- regression tests;
- task report;
- обновление functional Spark queue/progress.

Stage только явных paths. Никакого автоматического слияния.

## 9. Финальный результат

Работа заканчивается состоянием:

```text
FUNCTIONAL_CANDIDATE_FOR_REVIEW
```

Обязательные свойства:

- commits находятся только в `spark-functional/<RUN_ID>`;
- основной checkout и default branch не изменены;
- push/merge не выполнялись;
- bundle и затрагиваемые runtime copies проверены;
- финальная technical + operator validation выполнена;
- подготовлены:

```text
functional/spark/REMEDIATION_SUMMARY.md
functional/spark/POST_FIX_VALIDATION.md
functional/spark/POST_FIX_OPERATOR_ACCEPTANCE.md
functional/spark/ROLLBACK_INDEX.md
```

Пользователь сам решает, просматривать ли ветку, отправлять её на дополнительное review или сливать.