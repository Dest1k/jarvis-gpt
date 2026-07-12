# JARVIS — КАНОНИЧЕСКИЙ ЗАПУСК PHASE B НА ЖИВОЙ МАШИНЕ

Ты выполняешь PHASE B двухкомпонентного доказательного аудита JARVIS через Codex Sol Ultra на целевой Windows-машине.

Этот файл является **каноническим launcher-prompt**. Не заменяй его прямым запуском core-файла.

## 1. Начало

Рабочий репозиторий:

```text
D:\jarvis-gpt
```

Runtime/data root:

```text
D:\jarvis
```

Начни:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
git status --short --branch
```

Ожидаемый Git root — `D:/jarvis-gpt`. `D:\jarvis` не является репозиторием.

## 2. Обязательные документы

До любых runtime-действий полностью прочитай и затем исполни совместно:

1. `docs/audit/02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md` — полный core-план живого аудита;
2. `docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md` — обязательный safety/rollback-контур;
3. `.audit/LATEST_STATIC_RUN.txt` и весь handoff PHASE A, требуемый core-планом;
4. актуальные repository instructions (`AGENTS.md`, `CODEX.md`, `CLAUDE.md` и локальные эквиваленты).

Выполни **весь** scope core-плана. Этот launcher ничего из него не сокращает.

При конфликте:

- safety/rollback protocol имеет приоритет в вопросах Git, worktree, backups, runtime state, Docker, destructive/fault tests, restore и stop conditions;
- repository instructions имеют приоритет в локальных правилах проекта, если не ослабляют safety;
- не разрешай конфликт догадкой: выбери более безопасное поведение и зафиксируй решение.

## 3. Дополнительная обязательная цель PHASE B

После завершения испытательной кампании недостаточно создать обычную очередь Spark. Ты обязан подготовить **технически изолированный и проверяемо откатываемый remediation-контур**.

До создания обычного marker:

```text
.audit/runs/<RUN_ID>/spark/READY
```

выполни всё ниже.

### 3.1. Классифицируй все задачи Spark

Для каждой задачи заполни safety-поля из раздела 3 протокола:

- `mutation_class`;
- `mutable_roots`;
- `requires_stack_stop`;
- `requires_pre_task_snapshot`;
- `requires_restore_rehearsal`;
- `human_gate_required`;
- resource/process/network budgets;
- rollback checkpoint, commands и oracles.

Ни одна задача с неизвестным mutation scope не может иметь status READY.

### 3.2. Создай safety artifacts

В текущем run создай и проверь:

```text
spark/safety/SAFETY_PLAN.md
spark/safety/SAFETY_STATE.json
spark/safety/WORKTREE_IDENTITY.json
spark/safety/RUNTIME_MUTATION_MAP.csv
spark/safety/BACKUP_SCOPE.json
spark/safety/BACKUP_MANIFEST.json
spark/safety/PRE_SPARK_CHECKPOINT.json
spark/safety/RESTORE_RUNBOOK.md
spark/safety/RESTORE_VERIFICATION.md
spark/safety/TASK_CHECKPOINTS.jsonl
spark/safety/INCIDENT_LOG.md
```

### 3.3. Верни систему в known-good state

После всех fault/chaos/soak проверок:

1. выполни cleanup;
2. верни исходный или явно документированный профиль/runtime state;
3. запусти normal smoke;
4. проверь health, ports, containers, processes, GPU release и DB integrity;
5. сохрани конечный baseline, который станет исходной точкой Spark.

### 3.4. Подготовь отдельный remediation worktree

Не переключай пользовательскую ветку и не изменяй production-код в `D:\jarvis-gpt`.

Создай:

```text
D:\jarvis-gpt-worktrees\spark-<RUN_ID>
spark-remediation/<RUN_ID>
```

строго по разделу 4 safety protocol.

Если audit artifacts ещё не входят в base commit:

- скопируй в worktree только `.audit/runs/<RUN_ID>` и необходимые marker-файлы;
- проверь пути/hashes;
- создай audit-only snapshot commit в remediation branch;
- не stage пользовательские и production changes из исходного checkout.

Запиши точные repo/worktree paths, branch, base SHA и pre-remediation SHA в `WORKTREE_IDENTITY.json`.

### 3.5. Создай Git rollback assets

Создай и проверь:

- `pre-spark-source-<RUN_ID>`;
- `pre-spark-<RUN_ID>`;
- `D:\jarvis\audit-backups\<RUN_ID>\git\jarvis-gpt-pre-spark.bundle`;
- SHA-256 и `git bundle verify` evidence.

Не выполняй push, merge, rebase или force-update.

### 3.6. Создай verified runtime checkpoint

Для всех mutable critical objects, которые хотя бы одна READY-задача может затронуть:

- создай консистентную резервную копию;
- для SQLite используй backup API или доказуемо остановленных writers, а не слепую live-copy;
- инвентаризируй/экспортируй mutable Docker volumes либо заблокируй связанные задачи;
- проверь hashes/counts/integrity;
- выполни restore rehearsal в отдельный temp root;
- проверь доступное место до копирования;
- не копируй модели/images/rebuildable caches без необходимости.

Если checkpoint или restore rehearsal не доказаны, соответствующие state-mutating задачи получают `BLOCKED_BY_SAFETY`.

### 3.7. Safety consistency gate

Запусти отдельную consistency-проверку раздела 13 safety protocol.

Только при PASS:

1. установи `SAFETY_STATE.json.state = READY`;
2. создай пустой marker:

```text
.audit/runs/<RUN_ID>/spark/safety/READY
```

3. после этого создай обычный `spark/READY`;
4. создай `.audit/LATEST_COMPLETE_RUN.txt` только при выполнении всех остальных критериев core-плана.

Обычный `spark/READY` без `spark/safety/READY` запрещён.

## 4. Что передать Spark

Сгенерированный `spark/START_HERE_FOR_SPARK.md` должен требовать запуск через:

```text
docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
```

а не напрямую через core-файл.

Он должен явно указать:

- RUN_ID;
- remediation worktree и branch;
- audited/base/pre-spark commits;
- bundle path/hash;
- runtime checkpoint path/hash/status;
- safety state;
- known-good runtime state;
- первую eligible task;
- stop conditions.

## 5. Финальный ответ

В дополнение к core-отчёту сообщи:

- создан ли отдельный worktree и его точный путь;
- remediation branch и pre-Spark SHA;
- проверен ли Git bundle;
- создан и проверен ли runtime checkpoint;
- прошёл ли restore rehearsal;
- количество READY/BLOCKED_BY_SAFETY задач;
- существует ли `spark/safety/READY`;
- существует ли обычный `spark/READY`;
- в каком состоянии оставлены JARVIS, Docker и LLM.

Если safety gate не пройден, не называй PHASE C готовой к запуску и не создавай фиктивные READY markers.
