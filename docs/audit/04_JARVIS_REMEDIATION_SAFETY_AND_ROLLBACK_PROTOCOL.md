# JARVIS — ПРОТОКОЛ ИЗОЛЯЦИИ, РЕЗЕРВНОГО КОПИРОВАНИЯ И ОТКАТА PHASE B/C

Этот документ является обязательным дополнением к:

- `02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md`;
- `03_JARVIS_SPARK_REMEDIATION_PROMPT.md`.

При конфликте он имеет приоритет в вопросах Git, рабочих каталогов, резервных копий, изменяемого runtime-состояния, Docker, восстановления и аварийной остановки.

Цель — гарантировать, что ошибка агента, неудачный patch, регрессия или повреждение тестовой копии состояния не затронут `main`, исходный checkout и единственную копию пользовательских данных.

Текстовых запретов недостаточно. Поэтому обязательны проверяемые технические барьеры: отдельный Git worktree, отдельная ветка, теги, Git bundle, проверенный runtime checkpoint, task-level checkpoints, жёсткие stop conditions и отсутствие автоматического merge/push.

Это работа с принадлежащим пользователю локальным проектом. Не выполняй действия против внешних систем, не формируй инструкции по обходу ограничений и не используй реальные конфиденциальные данные. Все проверки границ выполняй только на безвредных синтетических примерах, локальных fixtures и копиях состояния.

---

## 1. Фиксированные каталоги

```text
D:\jarvis-gpt
  канонический checkout пользователя; не место для production-исправлений Spark

D:\jarvis-gpt-worktrees\spark-<RUN_ID>
  единственный допустимый worktree и рабочий каталог Spark

D:\jarvis
  модели, Docker/runtime-данные, пользовательское состояние, кеши и логи

D:\jarvis\audit-backups\<RUN_ID>\
  Git bundle, runtime checkpoints, manifests и доказательства восстановления

D:\jarvis\audit-evidence\<RUN_ID>\
  тяжёлые evidence-файлы аудита
```

`D:\jarvis` никогда не является Git-репозиторием. `D:\jarvis-gpt` никогда не является рабочим деревом для production-правок Spark.

Если фактическая конфигурация использует дополнительные изменяемые roots, PHASE B обязана обнаружить и добавить их в план. Нельзя молча ограничиваться перечисленными путями.

---

## 2. Состояния защитного контура

Используй только:

```text
LOCKED
CHECKPOINTING
READY
ACTIVE
BLOCKED
ABORTED
NEEDS_HUMAN_RESTORE
CANDIDATE_FOR_REVIEW
```

Spark не меняет production-код при `LOCKED`, `CHECKPOINTING`, `BLOCKED`, `ABORTED` или `NEEDS_HUMAN_RESTORE`.

В audit run должны существовать:

```text
.audit/runs/<RUN_ID>/spark/safety/
  SAFETY_PLAN.md
  SAFETY_STATE.json
  WORKTREE_IDENTITY.json
  RUNTIME_MUTATION_MAP.csv
  BACKUP_SCOPE.json
  BACKUP_MANIFEST.json
  PRE_SPARK_CHECKPOINT.json
  RESTORE_RUNBOOK.md
  RESTORE_VERIFICATION.md
  TASK_CHECKPOINTS.jsonl
  INCIDENT_LOG.md
  READY
```

Marker `spark/safety/READY` создаётся последним. Обычный `spark/READY` без него не разрешает PHASE C.

Минимальная схема `SAFETY_STATE.json`:

```json
{
  "schema_version": 1,
  "run_id": "...",
  "state": "LOCKED",
  "source_repo": "D:\\jarvis-gpt",
  "remediation_worktree": "D:\\jarvis-gpt-worktrees\\spark-<RUN_ID>",
  "remediation_branch": "spark-remediation/<RUN_ID>",
  "audited_source_commit": "...",
  "pre_spark_commit": null,
  "git_bundle_verified": false,
  "runtime_checkpoint_verified": false,
  "restore_rehearsal_passed": false,
  "created_at_utc": "...",
  "updated_at_utc": "..."
}
```

---

## 3. Что PHASE B обязана подготовить для каждой задачи

Каждая `SPARK-NNNN.md` получает поля:

```yaml
mutation_class: code_only | runtime_read_only | runtime_ephemeral |
  persistent_state_mutating | docker_topology_mutating | high_risk_blocked
mutable_roots: []
requires_stack_stop: true | false
requires_pre_task_snapshot: true | false
requires_restore_rehearsal: true | false
human_gate_required: true | false
max_runtime_minutes: 0
max_disk_growth_mb: 0
allowed_processes: []
allowed_containers: []
network_policy: offline | loopback_only | project_defined | external_read_only
rollback_checkpoint: ""
rollback_commands: []
rollback_oracles: []
```

READY запрещён, если:

- `mutation_class` не определён;
- изменяемые roots неизвестны;
- нет cleanup/rollback procedure;
- задача может менять долговременное состояние, но не требует snapshot;
- восстановление нельзя проверить наблюдаемым oracle;
- действие не изолировано;
- требуется решение пользователя о желаемом поведении;
- тест требует реальных конфиденциальных данных или воздействия на внешнюю систему.

### `RUNTIME_MUTATION_MAP.csv`

Минимальные столбцы:

```text
component,path_or_volume,data_class,owner,mutable,criticality,
backup_method,consistency_method,restore_method,restore_oracle,
excluded_reason,tasks,notes
```

Охвати:

- SQLite DB, WAL/SHM и migrations;
- uploaded/indexed files;
- document outputs;
- profile, memory, missions, approvals, learning и autonomy state;
- runtime configuration вне Git;
- named Docker volumes и bind mounts;
- browser/session state, если она долговечна;
- generated indexes/caches с указанием, можно ли их пересоздать;
- модели/images/cache с классификацией immutable/rebuildable/excluded.

### `BACKUP_SCOPE.json`

Для каждого объекта укажи:

- canonical source;
- data class;
- estimated size;
- free-space requirement;
- consistency strategy;
- backup destination;
- hash/integrity strategy;
- restore test;
- может ли конкретная Spark task его менять.

Если места недостаточно для проверенного checkpoint, соответствующие state-mutating задачи получают `BLOCKED_BY_SAFETY`. Code-only задачи могут остаться READY при исправных Git-страховках.

### Финальное known-good состояние PHASE B

Перед завершением живого аудита:

1. закончи все запланированные recovery/resource/long-run проверки;
2. выполни cleanup;
3. верни JARVIS к документированному known-good состоянию;
4. выполни normal smoke;
5. зафиксируй processes, containers, ports, profile, model, health и DB integrity;
6. зафиксируй состояние, которое Spark обязан сохранить или восстановить;
7. создай `RESTORE_RUNBOOK.md`;
8. не создавай `spark/READY`, пока artifacts не прошли consistency check.

PHASE B может создать предварительный runtime checkpoint. PHASE C всё равно проверяет его свежесть и при drift создаёт новый pre-Spark checkpoint.

---

## 4. Git-изоляция PHASE C

### 4.1. Канонический checkout остаётся нетронутым

В `D:\jarvis-gpt` PHASE C разрешено только:

- читать Git metadata и audit artifacts;
- проверять status;
- создавать branch/tag/worktree через Git;
- копировать текущие audit artifacts в отдельный worktree, если они ещё не committed;
- создавать Git bundle во внешнем backup root.

Запрещено:

- редактировать production/test/config files;
- выполнять formatter с записью;
- stage/commit production changes;
- переключать основную пользовательскую ветку;
- reset/clean/stash пользовательских изменений;
- запускать исправления Spark в каноническом checkout.

Если есть изменения вне `.audit/**` и `docs/audit/**`, которые нельзя однозначно отделить, PHASE C устанавливает `BLOCKED_BY_USER_CHANGES`.

### 4.2. Обязательный отдельный worktree

```powershell
$Repo = 'D:\jarvis-gpt'
$Worktree = "D:\jarvis-gpt-worktrees\spark-$RunId"
$Branch = "spark-remediation/$RunId"

git -C $Repo worktree add -b $Branch $Worktree $BaseSha
```

Перед выполнением:

- путь worktree не должен содержать чужие данные;
- существующую ветку/папку нельзя удалять или переиспользовать без проверки;
- если branch/worktree уже существует, сравни `WORKTREE_IDENTITY.json`, HEAD и RUN_ID;
- при несовпадении остановись;
- не используй `--force`.

Если `.audit` не входит в `$BaseSha`, скопируй только текущий run и marker-файлы, проверь hashes/paths и создай локальный audit-only commit. Не копируй несвязанные untracked files.

### 4.3. Проверка перед каждой задачей и commit

```powershell
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD
git status --short --branch
```

Требуется одновременно:

- top-level равен remediation worktree;
- branch равен `spark-remediation/<RUN_ID>`;
- source checkout не является текущим каталогом;
- нет неожиданных изменений;
- защитный state разрешает работу.

Несовпадение означает `ABORTED`.

---

## 5. Git checkpoints и независимая копия

До первого production patch создай:

1. annotated tag на audited source commit:

```text
pre-spark-source-<RUN_ID>
```

2. audit-only snapshot commit, если нужен;
3. annotated tag на pre-remediation HEAD:

```text
pre-spark-<RUN_ID>
```

4. Git bundle:

```text
D:\jarvis\audit-backups\<RUN_ID>\git\jarvis-gpt-pre-spark.bundle
```

Проверка:

```powershell
git -C $Worktree bundle create $BundlePath --all
git -C $Worktree bundle verify $BundlePath
```

Сохрани SHA-256, размер, UTC timestamp, included refs и результат проверки. Если bundle не создаётся или не проверяется, состояние остаётся `LOCKED`.

Перед каждой задачей создай annotated tag:

```text
pre-spark-<RUN_ID>-SPARK-NNNN
```

Если tag уже существует, он должен указывать на ожидаемый commit; не передвигай его.

После каждой успешной задачи — один локальный commit. Никаких `push`, merge, rebase, squash, force-update или переписывания истории.

---

## 6. Pre-Spark runtime checkpoint

### 6.1. Свежесть

Checkpoint можно переиспользовать только если не изменились:

- timestamps/hashes/counters изменяемых paths;
- DB identity/schema/integrity;
- Docker volume/container mapping;
- profile/runtime configuration;
- production commit;
- пользовательские данные после checkpoint.

При сомнении создай новый checkpoint непосредственно перед PHASE C.

### 6.2. Свободное место

Минимальный запас:

```text
estimated backup size × 1.25 + максимальный разрешённый рост задач + 10 GiB
```

Не заполняй системный или Docker-диск ради backup. При недостатке места блокируй state-mutating задачи.

### 6.3. SQLite

Не копируй работающую SQLite DB без доказательства consistency. Используй один из способов:

1. штатно остановить только JARVIS и убедиться, что writers завершились;
2. использовать `sqlite3.Connection.backup()`/SQLite backup API;
3. использовать документированный project-specific backup mechanism.

После backup:

- открой копию, не оригинал;
- выполни `PRAGMA integrity_check`;
- зафиксируй schema/user_version;
- проверь ключевые counts/invariants;
- сохрани SHA-256;
- докажи чтение из временного restore root.

Не выполняй repair/truncate оригинала только ради backup.

### 6.4. Файлы и каталоги

Копируй только ценное изменяемое состояние:

- не следуй junction/symlink/reparse points вслепую;
- исключи models, Docker images, rebuildable caches и большие evidence, если задачи их не меняют;
- фиксируй skipped/locked files;
- проверяй counts, sizes и hashes критических файлов;
- не называй backup полным при partial copy.

Uploaded/user files входят в backup, если хотя бы одна READY-задача может их изменить или удалить.

### 6.5. Docker volumes

Для каждого named volume определи, содержит ли он изменяемые критичные данные.

- cache/rebuildable volume можно документированно исключить;
- для critical volume останови только связанные JARVIS writers и сделай локальный export без загрузки новых образов;
- если проверенный export невозможен, связанные задачи получают `BLOCKED_BY_SAFETY`;
- не удаляй и не переименовывай рабочий volume ради проверки.

Сохрани inventory и metadata exports.

### 6.6. Пробное восстановление

Проверяй восстановление только в отдельный временный root/DB/volume, никогда поверх рабочей копии.

Минимальные oracles:

- backup manifest сходится;
- critical hashes/counts сходятся;
- SQLite integrity check PASS;
- restored config parses;
- restored files доступны;
- временный smoke не использует production state;
- cleanup не затронул рабочую систему.

Без успешной проверки persistent-state-mutating задачи остаются заблокированными.

---

## 7. Task-level checkpoint

Перед задачей с `requires_pre_task_snapshot: true`:

1. верни runtime в task-defined known state;
2. создай snapshot только затрагиваемых roots;
3. проверь snapshot;
4. добавь запись в `TASK_CHECKPOINTS.jsonl`;
5. сохрани restore command и oracle;
6. только затем запускай reproduction/patch.

Минимальная запись:

```json
{
  "task_id": "SPARK-0001",
  "git_tag": "pre-spark-<RUN_ID>-SPARK-0001",
  "git_head": "...",
  "runtime_snapshot": "D:\\jarvis\\audit-backups\\<RUN_ID>\\tasks\\SPARK-0001",
  "verified": true,
  "created_at_utc": "...",
  "restore_oracles": []
}
```

Проверки повреждённых/readonly/nearly-full состояний выполняются только на task snapshot, synthetic root или test volume. Рабочая DB/volume не используется как тестовый объект.

---

## 8. Неудача текущей задачи

Если patch ещё не committed и validation провалилась:

1. останови только процессы текущей задачи;
2. сохрани evidence и task report;
3. восстанови только явные `allowed_files` из task tag:

```powershell
git restore --source=<TAG> -- <explicit paths>
```

4. не используй `git reset --hard`;
5. при runtime mutation восстанови task snapshot и проверь oracles;
6. пометь точный blocker;
7. не оставляй частичный patch следующей задаче.

Если задача уже committed, а batch validation обнаружила регрессию:

- не переписывай историю;
- установи responsible commit;
- создай отдельный `git revert <SHA>`, если откат проверяем;
- либо создай узкую follow-up task;
- повтори regression и runtime smoke;
- зафиксируй task → commit → revert mapping.

Если автоматическое восстановление не доказано, установи `NEEDS_HUMAN_RESTORE` и останови state-mutating задачи.

---

## 9. Немедленные stop conditions

Остановись при любом условии:

- Git root/branch не соответствует worktree identity;
- обнаружены изменения PHASE C в `D:\jarvis-gpt`;
- найден необъяснимый dirty diff;
- задача выходит за `allowed_files` или mutable roots;
- команда требует broad delete/prune/reset/clean/factory reset;
- backup/hash verification перестали проходить;
- DB integrity check не PASS;
- пробное восстановление не PASS;
- затронут посторонний process/container/volume/network;
- свободное место ниже рассчитанного floor;
- runtime/resource growth превышает budget;
- требуется незапланированная загрузка или обновление model/image/dependency;
- фактическое действие шире task contract;
- найден новый критичный риск потери данных или нарушения границ доступа;
- cleanup не возвращает known-good state;
- невозможно определить, какие данные изменены.

Действия:

1. прекратить новые stimuli;
2. не маскировать ошибку;
3. обновить `INCIDENT_LOG.md`;
4. установить `ABORTED` или `NEEDS_HUMAN_RESTORE`;
5. восстановить только то, для чего есть проверенный checkpoint;
6. выполнить integrity/smoke check;
7. не удалять evidence, worktree, branch, tags или backups.

---

## 10. Resource и process guardrails

Каждая runtime-задача должна иметь:

- timeout/watchdog;
- max disk growth;
- max RAM/VRAM expectation;
- список допустимых processes/containers;
- cleanup deadline;
- правила cancel;
- начальный и конечный health oracle.

Запрещено:

- завершать процессы по широкому имени без PID ownership;
- останавливать все Docker containers;
- менять или удалять чужую WSL distribution;
- делать системные сетевые изменения без scoped reversible rule и exact cleanup;
- запускать неограниченные random/long-run проверки;
- создавать unlimited logs/dumps;
- завершать process, не подтверждённый как JARVIS-owned.

---

## 11. Итог PHASE C не сливается автоматически

PHASE C заканчивается:

```text
CANDIDATE_FOR_REVIEW
```

Обязательные свойства:

- commits находятся только в `spark-remediation/<RUN_ID>`;
- `main`/исходная ветка не переключалась и не менялась;
- push и merge не выполнялись;
- worktree, branch, tags, bundle и backups сохранены;
- финальная validation выполнена;
- runtime возвращён к документированному состоянию;
- `spark/REMEDIATION_SUMMARY.md` содержит task → commit → rollback mapping;
- `spark/POST_FIX_VALIDATION.md` содержит реальные проверки;
- `spark/ROLLBACK_INDEX.md` содержит команды полного и частичного отката;
- unresolved/blockers перечислены честно.

Пользователь получает ветку-кандидат и сам решает, проводить ли дополнительный review и merge.

---

## 12. `ROLLBACK_INDEX.md`

### Отказ от всей работы Spark

```powershell
git -C D:\jarvis-gpt status --short --branch
```

Ничего не сливай. Remediation branch/worktree сохраняются для анализа.

### Откат одной committed задачи

```powershell
git revert <TASK_COMMIT_SHA>
```

После revert обязательны task regression, соседний suite и runtime smoke.

### Восстановление Git из bundle

Опиши проверенный clone/fetch из:

```text
D:\jarvis\audit-backups\<RUN_ID>\git\jarvis-gpt-pre-spark.bundle
```

Не восстанавливай автоматически поверх существующего репозитория.

### Восстановление runtime state

Укажи:

- требуемый stopped state;
- exact backup path;
- exact restore commands;
- какие working paths сначала переименовываются/изолируются, а не удаляются;
- DB/file/volume integrity oracles;
- normal smoke;
- возврат в первоначальный profile/state.

Никаких restore-команд с широкими wildcard delete.

---

## 13. Consistency gate перед `spark/safety/READY`

Проверка отклоняет READY, если:

- нет отдельного worktree plan;
- remediation branch совпадает с default branch;
- нет audited source SHA;
- нет Git bundle plan и verification oracle;
- не классифицирована хотя бы одна Spark task;
- state-mutating task не имеет snapshot/restore fields;
- критичный mutable object отсутствует в mutation map;
- недостаточно свободного места;
- backup partial/unverified;
- restore rehearsal failed/not run для relevant tasks;
- обычный `spark/READY` существует без safety READY;
- cleanup/known-good state PHASE B не доказан;
- есть high-risk task без human gate или изоляции;
- есть broken paths/unknown IDs.

Только после PASS:

1. установи `SAFETY_STATE.json.state = READY`;
2. создай `spark/safety/READY`;
3. затем разреши создание обычного `spark/READY`;
4. зафиксируй exact worktree, branch, SHAs, bundle, backup и known-good state.

---

## 14. Формат сообщений пользователю

Все сообщения пользователю пиши по-русски.

В чате не публикуй:

- большие raw-логи;
- содержимое конфиденциальных файлов;
- длинные наборы тестовых входов;
- подробные operational инструкции по нарушению границ.

Сохраняй подробности локально в audit artifacts. В чате сообщай только status, процент готовности, counts, paths, blockers и следующие безопасные действия.
