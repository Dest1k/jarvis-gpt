# JARVIS — КАНОНИЧЕСКИЙ БЕЗОПАСНЫЙ ЗАПУСК CODEX SPARK

Ты выполняешь PHASE C: контролируемое исправление подтверждённых результатов двухкомпонентного аудита JARVIS.

Этот файл является **единственным каноническим launcher-prompt для Spark**. Он объединяет task-loop с обязательной технической изоляцией, резервными копиями и проверяемым откатом.

Это работа с принадлежащим пользователю локальным проектом. Не воздействуй на внешние системы, не формируй инструкции по нарушению ограничений и не используй реальные конфиденциальные данные. Для задач ввода, разрешений, URL и файловых границ применяй только harmless synthetic examples, loopback fixtures, temp roots и copied state.

Все сообщения пользователю пиши только по-русски. Подробности сохраняй в task reports. В чате сообщай процент готовности, counts, paths, blockers и commit SHA. Плановые отчёты — на 50% и 90%, а также после каждого batch.

---

## 1. Сначала только preflight в каноническом checkout

Начни:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
git status --short --branch
git rev-parse HEAD
```

Ожидаемый Git root — `D:/jarvis-gpt`.

В `D:\jarvis-gpt` запрещено исправлять production-код. Этот checkout используется только для чтения audit artifacts и проверки/создания remediation worktree.

Не выполняй reset/clean/stash/rebase для пользовательских изменений.

---

## 2. Обязательные документы

Полностью прочитай и исполняй совместно:

1. `docs/audit/03_JARVIS_SPARK_REMEDIATION_PROMPT.md` — core task-loop;
2. `docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md` — isolation/backup/rollback;
3. `.audit/LATEST_COMPLETE_RUN.txt` и указанный run;
4. `spark/START_HERE_FOR_SPARK.md`;
5. `spark/SPARK_MASTER_PROMPT.md`;
6. `spark/SPARK_QUEUE.csv`;
7. `spark/SPARK_PROGRESS.md`;
8. `spark/TASK_SCHEMA.md`;
9. весь `spark/safety/`;
10. актуальные repository instructions.

При конфликте rollback protocol имеет приоритет в Git root/worktree/branch, backups, runtime state, Docker, restore, cleanup и stop conditions.

Все patch/test/commit действия выполняются только в remediation worktree.

---

## 3. Жёсткий gate

В run должны существовать одновременно:

```text
spark/READY
spark/safety/READY
```

`SAFETY_STATE.json` должен показывать `state = READY`, а `PIPELINE_STATE.json` — завершённые PHASE A/PHASE B и разрешённые Spark tasks.

Если хотя бы одно условие не выполнено:

- не создавай production patch;
- не заменяй PHASE B;
- зафиксируй blocker;
- остановись.

---

## 4. Проверка изоляции

Из `WORKTREE_IDENTITY.json` получи:

```text
remediation worktree: D:\jarvis-gpt-worktrees\spark-<RUN_ID>
remediation branch:   spark-remediation/<RUN_ID>
base/source SHA
pre-spark SHA
```

Проверь:

```powershell
git -C D:\jarvis-gpt status --short --branch
git -C $Worktree rev-parse --show-toplevel
git -C $Worktree branch --show-current
git -C $Worktree rev-parse HEAD
git -C $Worktree status --short --branch
git -C D:\jarvis-gpt worktree list --porcelain
```

Обязательные условия:

- канонический checkout не содержит изменений PHASE C;
- worktree совпадает с `WORKTREE_IDENTITY.json`;
- branch равен `spark-remediation/<RUN_ID>`;
- branch не является default branch;
- HEAD соответствует pre-Spark state;
- folder/branch принадлежат текущему run;
- нет необъяснимого dirty diff.

Если worktree не подготовлен PHASE B, разрешено создать его только по rollback protocol и после проверки остальных safety artifacts. Не используй `--force` и не удаляй существующую папку/ветку.

После проверки:

```powershell
Set-Location $Worktree
```

Больше не редактируй исходный `D:\jarvis-gpt`.

---

## 5. Проверка Git rollback assets

До первой task проверь:

- tag `pre-spark-source-<RUN_ID>`;
- tag `pre-spark-<RUN_ID>`;
- bundle path из manifest;
- SHA-256 bundle;
- `git bundle verify`;
- included refs;
- соответствие bundle текущему run/source SHA.

Если bundle отсутствует, повреждён или устарел, создай новый verified bundle до patch. При невозможности state остаётся LOCKED.

Не передвигай safety tags и не используй force.

---

## 6. Проверка runtime checkpoint

Сопоставь текущие mutable state, DB, config и Docker mappings с `PRE_SPARK_CHECKPOINT.json`.

Если critical state изменилось или freshness не доказана:

1. верни JARVIS к documented known-good state;
2. создай новый pre-Spark checkpoint;
3. проверь свободное место;
4. выполни DB/file/volume integrity checks;
5. выполни restore rehearsal в temp root;
6. обнови manifests/hashes;
7. только затем верни safety state в READY.

Если checkpoint невозможно создать/проверить:

- code-only tasks могут выполняться при исправных Git safeguards;
- state-mutating tasks получают `BLOCKED_BY_SAFETY`;
- не ослабляй требования ради продолжения.

---

## 7. Возобновление после оборванной сессии Spark

Если `SPARK_PROGRESS.md` уже содержит выполненные tasks:

1. проверь worktree, branch, HEAD и task → commit mapping;
2. проверь, что canonical checkout не менялся;
3. проверь последние batch validation и runtime known-good state;
4. сопоставь task tags/checkpoints;
5. не повторяй committed tasks;
6. при незавершённом dirty patch восстанови только explicit allowed files из task tag и task snapshot;
7. обнови `RESUME_NOTE.md`;
8. продолжи с первой eligible READY task.

Если происхождение dirty diff или runtime mutation неизвестно, установи `ABORTED`/`NEEDS_HUMAN_RESTORE` и остановись.

---

## 8. Механический guard перед каждой task

Перед выбором и непосредственно перед commit:

```powershell
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD
git status --short --branch
```

Проверь:

- top-level = remediation worktree;
- branch = `spark-remediation/<RUN_ID>`;
- safety state разрешает работу;
- task eligible;
- нет unexpected changes;
- canonical checkout не изменён;
- runtime соответствует preconditions;
- resource/disk budgets доступны.

Любое несовпадение — `ABORTED` без попытки «быстро закончить».

---

## 9. Дополнение обязательного task-loop

Выполняй весь цикл core-файла, добавляя перед Baseline:

### 9.1. Task Git checkpoint

Создай annotated tag:

```text
pre-spark-<RUN_ID>-SPARK-NNNN
```

Он указывает на HEAD до task. Существующий tag не передвигай.

### 9.2. Task runtime checkpoint

Если `requires_pre_task_snapshot: true`:

- создай snapshot затрагиваемых mutable roots;
- проверь snapshot/restore oracle;
- запиши его в `TASK_CHECKPOINTS.jsonl`;
- не запускай reproduction до verification.

### 9.3. Scope guard

Зафиксируй:

- files/symbols;
- processes/containers;
- mutable roots;
- network mode;
- disk/runtime budgets;
- cleanup/rollback commands.

Выход за scope блокирует task. Не расширяй scope самостоятельно.

### 9.4. Neutral reproduction rule

Reproduction и regression test должны быть функциональными и безвредными:

- synthetic values;
- local/loopback fixtures;
- temp roots/copied state;
- no-op actions where possible;
- отсутствие реальных конфиденциальных данных;
- отсутствие воздействия на внешние системы;
- отсутствие operational instructions по нарушению границ.

Task, которую нельзя воспроизвести таким способом, получает `BLOCKED_BY_POLICY` или `BLOCKED_BY_SAFETY`.

---

## 10. Если task не удалась

До commit:

1. останови только task-owned processes;
2. сохрани evidence/report;
3. восстанови только explicit `allowed_files` из task tag;
4. не используй `reset --hard`;
5. при runtime mutation восстанови verified task snapshot;
6. проверь rollback oracles и normal smoke;
7. пометь blocker;
8. не оставляй partial patch следующей task.

После commit, если batch validation нашла регрессию:

- не переписывай историю;
- найди responsible commit;
- используй отдельный `git revert <SHA>`, если rollback доказуем;
- либо создай узкую follow-up task;
- повтори validation;
- зафиксируй bad/revert commits.

Если automatic restore не доказан, установи `NEEDS_HUMAN_RESTORE` и останови state-mutating tasks.

---

## 11. Немедленные stop conditions

Особенно:

- wrong root/branch;
- изменения в `D:\jarvis-gpt`;
- dirty diff неизвестного происхождения;
- broad delete/prune/reset/clean;
- backup/DB/restore verification failure;
- неожиданный Docker/WSL/process impact;
- low disk/resource runaway;
- task scope violation;
- незапланированная загрузка/update models/images/dependencies;
- невозможность определить изменённые данные;
- cleanup не вернул known-good state.

При stop condition не удаляй worktree, branch, tags, bundle, backups или evidence.

---

## 12. Commit policy

Для каждой успешной task:

- один локальный commit;
- stage только explicit paths;
- fix + regression tests + task report + queue/progress update;
- никаких `git add -A` в dirty tree;
- никаких push/merge/rebase/squash/force;
- после commit — status, SHA и task → commit mapping.

Canonical checkout и default branch не изменяются.

---

## 13. Batch/final validation

Перед переходом между batches:

- все tasks имеют конечный status;
- validation checkpoint PASS;
- responsible commits известны;
- runtime known-good;
- DB/file/volume integrity не ухудшилась;
- resource leaks отсутствуют или оформлены blocker;
- progress/safety state обновлены.

После последнего batch создай:

```text
spark/REMEDIATION_SUMMARY.md
spark/POST_FIX_VALIDATION.md
spark/ROLLBACK_INDEX.md
```

Финальный state:

```text
CANDIDATE_FOR_REVIEW
```

Не merge и не push.

---

## 14. Формат сообщений пользователю

На 50%, 90% и после каждого batch кратко сообщай:

- процент;
- DONE/BLOCKED/READY counts;
- current batch/task;
- current HEAD;
- runtime known-good status;
- blockers, если есть.

Не публикуй raw logs, содержимое confidential files, длинные test inputs или operational details по нарушению границ.

---

## 15. Финальный ответ

Кратко по-русски сообщи:

- run path;
- worktree/branch/final HEAD;
- DONE/BLOCKED/OBSOLETE/remaining READY counts;
- task → commit mapping;
- batch/final validation results;
- profiles/runtime/GUI checks;
- bundle/checkpoint/restore status;
- unresolved findings/residual risks;
- конечное состояние JARVIS/Docker/LLM;
- paths к progress/summary/post-fix/rollback documents;
- state `CANDIDATE_FOR_REVIEW`.

Не говори «всё исправлено», если остались blockers или непроверенные acceptance checks.
