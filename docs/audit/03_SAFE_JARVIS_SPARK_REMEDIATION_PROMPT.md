# JARVIS — КАНОНИЧЕСКИЙ БЕЗОПАСНЫЙ ЗАПУСК CODEX SPARK

Ты выполняешь PHASE C: исправление подтверждённых результатов двухкомпонентного аудита JARVIS.

Этот файл является **единственным каноническим launcher-prompt для Spark**. Он не сокращает core-процесс, а добавляет обязательную техническую изоляцию, резервные копии и проверяемый откат.

## 1. Сначала только preflight в каноническом checkout

Начни в:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
git status --short --branch
```

Ожидаемый Git root — `D:/jarvis-gpt`.

В `D:\jarvis-gpt` запрещено исправлять production-код. Этот checkout используется только для чтения audit artifacts и проверки/создания safety worktree.

## 2. Обязательные документы

Полностью прочитай и исполняй совместно:

1. `docs/audit/03_JARVIS_SPARK_REMEDIATION_PROMPT.md` — core-цикл задач;
2. `docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md` — обязательный safety/rollback-контур;
3. `.audit/LATEST_COMPLETE_RUN.txt` и указанный run;
4. `spark/START_HERE_FOR_SPARK.md`;
5. `spark/SPARK_MASTER_PROMPT.md`;
6. `spark/SPARK_QUEUE.csv`;
7. `spark/SPARK_PROGRESS.md`;
8. `spark/TASK_SCHEMA.md`;
9. весь каталог `spark/safety/`;
10. актуальные repository instructions (`AGENTS.md`, `CODEX.md`, `CLAUDE.md` и локальные эквиваленты).

При конфликте safety protocol имеет приоритет в вопросах Git root/worktree/branch, backups, runtime state, Docker, restore, cleanup и аварийной остановки.

В частности, указание core-файла работать в `D:\jarvis-gpt` относится только к первоначальному preflight. Все patch/test/commit действия выполняются исключительно в отдельном remediation worktree.

## 3. Жёсткий gate: не начинай без двух READY markers

В run должны существовать одновременно:

```text
spark/READY
spark/safety/READY
```

Кроме того, `SAFETY_STATE.json` должен показывать `state = READY`, а `PIPELINE_STATE.json` — завершённые PHASE A/PHASE B и разрешённые Spark tasks.

Если хотя бы одно условие не выполнено:

- не создавай production patch;
- не пытайся самовольно заменить PHASE B;
- зафиксируй точный blocker;
- остановись.

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

- канонический checkout не содержит новых изменений, созданных PHASE C;
- текущий worktree совпадает с `WORKTREE_IDENTITY.json`;
- branch строго равен `spark-remediation/<RUN_ID>`;
- branch не является `main`, `master` или default branch;
- HEAD соответствует допустимому pre-Spark state;
- существующая папка/branch не принадлежат другому run;
- нет необъяснимого dirty diff.

Если worktree не был подготовлен PHASE B, разрешено создать его только строго по разделу 4 safety protocol и только после проверки всех остальных safety artifacts. Не используй `--force` и не удаляй существующую папку/ветку.

После проверки установи рабочий каталог:

```powershell
Set-Location $Worktree
```

Больше не редактируй исходный `D:\jarvis-gpt`.

## 5. Повторная проверка Git rollback assets

До первой задачи проверь:

- tag `pre-spark-source-<RUN_ID>`;
- tag `pre-spark-<RUN_ID>`;
- bundle path из manifest;
- SHA-256 bundle;
- `git bundle verify`;
- included refs;
- соответствие bundle текущему run и source SHA.

Если bundle отсутствует, повреждён, устарел или относится к другому run, создай новый verified bundle до любого production patch. При невозможности состояние остаётся LOCKED.

Не передвигай существующие safety tags и не используй force.

## 6. Повторная проверка runtime checkpoint

Сопоставь текущие mutable state, DB, config и Docker mappings с `PRE_SPARK_CHECKPOINT.json`.

Если после checkpoint изменилось хотя бы одно критическое состояние либо свежесть нельзя доказать:

1. верни JARVIS к документированному known-good state;
2. создай новый pre-Spark runtime checkpoint по разделу 6 safety protocol;
3. проверь свободное место;
4. выполни SQLite/file/volume integrity checks;
5. выполни restore rehearsal в отдельный temp root;
6. обнови manifests и hashes;
7. только затем установи safety state READY.

Если checkpoint невозможно создать или проверить:

- code-only задачи могут выполняться при исправных Git-страховках;
- задачи `persistent_state_mutating`/`docker_topology_mutating` получают `BLOCKED_BY_SAFETY`;
- не ослабляй требования ради продолжения очереди.

## 7. Механический guard перед каждой задачей

Перед выбором и непосредственно перед commit каждой задачи выполни:

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
- текущая task eligible;
- нет неожиданных изменений;
- нет изменений в каноническом checkout;
- runtime соответствует task preconditions;
- resource/disk budgets доступны.

Любое несовпадение — `ABORTED`, без попытки «быстро закончить».

## 8. Дополнение обязательного task-цикла

Выполняй весь цикл из core-файла, но перед его шагом Baseline добавь:

### 8.1. Task Git checkpoint

Создай annotated tag:

```text
pre-spark-<RUN_ID>-SPARK-NNNN
```

Tag должен указывать на HEAD до task. Если он существует, проверь SHA; не передвигай force.

### 8.2. Task runtime checkpoint

Если `requires_pre_task_snapshot: true`:

- создай snapshot только затрагиваемых mutable roots;
- проверь snapshot и restore oracle;
- добавь запись в `spark/safety/TASK_CHECKPOINTS.jsonl`;
- не запускай reproduction, пока snapshot не verified.

### 8.3. Scope guard

До patch зафиксируй разрешённые:

- files/symbols;
- processes/containers;
- mutable roots;
- network mode;
- disk/runtime budgets;
- cleanup/rollback commands.

Выход за scope блокирует задачу. Не расширяй scope самостоятельно.

## 9. Если task не удалась

До commit:

1. останови только task-owned процессы;
2. сохрани evidence/report;
3. восстанови только явные `allowed_files` из task tag;
4. не используй `git reset --hard`;
5. при runtime mutation восстанови verified task snapshot;
6. проверь rollback oracles и normal smoke;
7. пометь точный blocker;
8. не оставляй полусломанный patch следующей задаче.

После commit, если batch validation нашла регрессию:

- не переписывай историю;
- найди responsible commit;
- используй отдельный `git revert <SHA>`, только если rollback доказуем;
- либо создай узкую follow-up task;
- повтори validation и зафиксируй bad/revert commits.

Если автоматический restore не доказан, установи `NEEDS_HUMAN_RESTORE` и останови все state-mutating tasks.

## 10. Немедленные stop conditions

Применяй полный список раздела 9 safety protocol. Особенно:

- wrong root/branch;
- изменения в `D:\jarvis-gpt`;
- dirty diff неизвестного происхождения;
- broad delete/prune/reset/clean;
- backup/DB/restore verification failure;
- неожиданный Docker/WSL/process impact;
- low disk/resource runaway;
- task scope violation;
- download/update моделей или images вне задачи;
- невозможность определить изменённые данные;
- cleanup не вернул known-good state.

При stop condition не удаляй worktree, branch, tags, bundle, backups или evidence.

## 11. Commit policy

Для каждой успешной task:

- один локальный commit;
- stage только явных путей;
- production fix + regression tests + task report + queue/progress update;
- никаких `git add -A` в грязном дереве;
- никаких push/merge/rebase/squash/force;
- после commit — status, SHA и task → commit mapping.

Канонический checkout и default branch не изменяются.

## 12. Batch и final validation

Перед переходом между batches:

- все task имеют конечный status;
- validation checkpoint PASS;
- responsible commits известны;
- runtime known-good;
- DB/file/volume integrity не ухудшилась;
- resource leaks отсутствуют либо оформлены finding/blocker;
- `SPARK_PROGRESS.md` и safety state обновлены.

После последнего batch выполни полный предусмотренный post-fix validation. Старый audit PASS не заменяет повторную проверку.

## 13. Финал — только кандидат на review

Не выполняй merge и push. Итоговое состояние:

```text
CANDIDATE_FOR_REVIEW
```

Создай/обнови:

```text
spark/REMEDIATION_SUMMARY.md
spark/POST_FIX_VALIDATION.md
spark/ROLLBACK_INDEX.md
spark/safety/SAFETY_STATE.json
spark/safety/INCIDENT_LOG.md
```

`REMEDIATION_SUMMARY.md` должен содержать:

- task → fix commit;
- task → pre-task tag;
- task → optional revert commit;
- changed files;
- tests;
- runtime effects;
- rollback checkpoint;
- blockers/residual risks.

`ROLLBACK_INDEX.md` должен дать:

- отказ от всей remediation branch без изменения main;
- `git revert` для каждой отдельной задачи;
- проверенный путь Git bundle;
- runtime restore paths/commands/oracles;
- предупреждение не выполнять destructive restore поверх рабочей копии автоматически.

Оставь:

- remediation worktree;
- remediation branch;
- safety tags;
- Git bundle;
- runtime backups;
- evidence.

Ничего из этого автоматически не удаляй.

## 14. Финальный ответ

Сообщи:

- RUN_ID;
- remediation worktree/branch/final HEAD;
- подтверждение, что `D:\jarvis-gpt` и default branch не изменялись;
- Git bundle path/hash/verify result;
- runtime checkpoint и restore rehearsal status;
- DONE/BLOCKED/OBSOLETE/remaining READY;
- task → commit → rollback tag/revert mapping;
- batch/final validation;
- конечный runtime/Docker/LLM state;
- safety state;
- пути к `REMEDIATION_SUMMARY.md`, `POST_FIX_VALIDATION.md` и `ROLLBACK_INDEX.md`;
- явную фразу, что merge/push не выполнялись и результат является кандидатом на человеческий review.

Если остались blockers, непроверенные high-risk areas, failed restore oracle или unknown mutation, не называй работу безопасно завершённой.
