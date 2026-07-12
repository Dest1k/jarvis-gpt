# JARVIS — ОБЯЗАТЕЛЬНЫЙ ПРОТОКОЛ БЕЗОПАСНОСТИ И ОТКАТА PHASE B/C

Этот документ является обязательным дополнением к:

- `02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md`;
- `03_JARVIS_SPARK_REMEDIATION_PROMPT.md`.

При конфликте формулировок **этот протокол имеет приоритет в вопросах Git, рабочих каталогов, резервных копий, mutable runtime state, Docker, восстановления и аварийной остановки**.

Цель — сделать так, чтобы ошибка Spark, неудачный patch, неверная команда, регрессия или повреждение runtime-состояния не затронули `main`, исходный checkout и единственную копию пользовательских данных.

Текстовые запреты не являются достаточной защитой сами по себе. Поэтому ниже требуются проверяемые технические барьеры: отдельный Git worktree, отдельная ветка, теги, Git bundle, consistency-checked runtime checkpoint, task-level checkpoints, жёсткие stop conditions и отсутствие автоматического merge/push.

---

## 1. Фиксированные каталоги

```text
D:\jarvis-gpt
  канонический checkout пользователя; не место для исправлений Spark

D:\jarvis-gpt-worktrees\spark-<RUN_ID>
  единственный допустимый worktree и working directory Spark

D:\jarvis
  модели, Docker/runtime-данные, пользовательское состояние, кеши и логи

D:\jarvis\audit-backups\<RUN_ID>\
  Git bundle, runtime checkpoints, manifests и restore evidence

D:\jarvis\audit-evidence\<RUN_ID>\
  тяжёлые evidence-файлы аудита
```

`D:\jarvis` никогда не является Git-репозиторием. `D:\jarvis-gpt` никогда не является рабочим деревом для production-правок Spark.

Если фактическая конфигурация использует дополнительные mutable roots, PHASE B обязана обнаружить и добавить их в safety plan. Нельзя молча ограничиваться перечисленными путями.

---

## 2. Состояния safety-контурa

Используй только следующие состояния:

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

Spark не имеет права менять production-код при состоянии `LOCKED`, `CHECKPOINTING`, `BLOCKED`, `ABORTED` или `NEEDS_HUMAN_RESTORE`.

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

Marker `spark/safety/READY` создаётся последним. Наличие обычного `spark/READY` без `spark/safety/READY` не разрешает PHASE C.

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

## 3. Обязанности PHASE B перед разблокировкой Spark

PHASE B обязана не только создать очередь задач, но и подготовить доказуемый план её безопасного исполнения.

### 3.1. Классификация каждой задачи

Каждая `SPARK-NNNN.md` получает обязательные поля:

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
- mutable roots неизвестны;
- нет cleanup/rollback procedure;
- задача может менять persistent state, но не требует task snapshot;
- rollback нельзя проверить наблюдаемым oracle;
- high-risk действие не изолировано;
- требуется product decision или неясный destructive scope.

### 3.2. `RUNTIME_MUTATION_MAP.csv`

Минимальные столбцы:

```text
component,path_or_volume,data_class,owner,mutable,criticality,
backup_method,consistency_method,restore_method,restore_oracle,
excluded_reason,tasks,notes
```

Нужно охватить:

- SQLite DB, WAL/SHM и migrations;
- uploaded/indexed files;
- document outputs;
- user/operator profile, memory, missions, approvals, learning и autonomy state;
- runtime configuration вне Git;
- named Docker volumes;
- bind mounts;
- browser/session state, если она долговечна;
- generated indexes/caches, указав, можно ли их безопасно пересоздать;
- модели/images/cache, явно классифицировав их как immutable/re-downloadable/excluded.

### 3.3. `BACKUP_SCOPE.json`

Для каждого объекта укажи:

- canonical source;
- data class;
- estimated size;
- free-space requirement;
- consistency strategy;
- backup destination;
- hash/integrity strategy;
- restore test;
- whether Spark tasks may mutate it.

Если свободного места недостаточно для доказуемого checkpoint, задачи, которые могут затронуть соответствующие данные, должны получить `BLOCKED_BY_SAFETY`. Code-only задачи могут остаться READY при выполненных Git-страховках.

### 3.4. Финальное known-good состояние PHASE B

Перед завершением живого аудита:

1. закончи fault/chaos/soak tests;
2. выполни cleanup;
3. верни JARVIS к документированному known-good состоянию;
4. выполни normal smoke;
5. зафиксируй processes, containers, ports, profile, model, health и DB integrity;
6. зафиксируй состояние, которое Spark обязан сохранить или восстановить;
7. создай `RESTORE_RUNBOOK.md`;
8. не создавай `spark/READY`, пока safety artifacts не прошли consistency check.

PHASE B может создать предварительный runtime checkpoint. PHASE C всё равно обязана проверить его свежесть и при любом drift создать новый pre-Spark checkpoint.

---

## 4. Git-изоляция PHASE C

### 4.1. Канонический checkout остаётся нетронутым

В `D:\jarvis-gpt` PHASE C разрешено только:

- читать Git metadata и audit artifacts;
- проверять status;
- создавать branch/tag/worktree через Git;
- копировать audit artifacts в отдельный worktree, если они ещё не committed;
- создавать Git bundle во внешнем backup root.

Запрещено в `D:\jarvis-gpt`:

- редактировать production/test/config files;
- выполнять formatter с записью;
- stage/commit production changes;
- switch основной пользовательской ветки;
- reset/clean/stash пользовательских изменений;
- запускать исправления Spark.

Если есть изменения вне `.audit/**` и `docs/audit/**`, которые невозможно однозначно отделить, PHASE C устанавливает `BLOCKED_BY_USER_CHANGES`. Она не присваивает их себе и не копирует в remediation branch.

### 4.2. Обязательный отдельный worktree

Допустимый шаблон:

```powershell
$Repo = 'D:\jarvis-gpt'
$Worktree = "D:\jarvis-gpt-worktrees\spark-$RunId"
$Branch = "spark-remediation/$RunId"

# BASE_SHA берётся из объединённого аудита после проверки source drift.
git -C $Repo worktree add -b $Branch $Worktree $BaseSha
```

Перед выполнением:

- путь worktree не должен содержать чужие данные;
- существующую ветку/папку нельзя force-delete;
- если branch/worktree уже существует, проверить `WORKTREE_IDENTITY.json`, HEAD и RUN_ID;
- при несовпадении остановиться, а не переиспользовать его;
- не использовать `--force`.

Если `.audit` не входит в `$BaseSha`, скопируй только текущий run из канонического checkout в remediation worktree, проверь hashes/paths и создай первый локальный audit-only commit. Не копируй несвязанные untracked files.

### 4.3. Обязательная проверка перед каждой задачей

Перед каждым task и commit:

```powershell
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD
git status --short --branch
```

Требуется одновременно:

- top-level равен remediation worktree;
- branch строго равен `spark-remediation/<RUN_ID>`;
- source checkout не является текущим каталогом;
- нет неожиданных изменений;
- safety state разрешает работу.

Несовпадение — немедленный `ABORTED`.

---

## 5. Git checkpoints и независимая копия репозитория

До первого production patch создай:

1. annotated tag на исходном audited source commit:

```text
pre-spark-source-<RUN_ID>
```

2. audit-only snapshot commit в remediation branch, если audit artifacts не были committed;
3. annotated tag на pre-remediation HEAD:

```text
pre-spark-<RUN_ID>
```

4. Git bundle:

```text
D:\jarvis\audit-backups\<RUN_ID>\git\jarvis-gpt-pre-spark.bundle
```

Пример:

```powershell
git -C $Worktree bundle create $BundlePath --all
git -C $Worktree bundle verify $BundlePath
```

Сохрани SHA-256 bundle, размер, UTC timestamp, included refs и результат `bundle verify`.

Если bundle не создаётся или не верифицируется, состояние остаётся `LOCKED`.

Перед каждой задачей создай annotated task tag:

```text
pre-spark-<RUN_ID>-SPARK-NNNN
```

Если tag уже существует, он должен указывать на ожидаемый commit; никогда не передвигай его force.

После каждой успешной задачи — один обычный локальный commit. Никаких `push`, merge, rebase, squash, force-update или переписывания истории.

---

## 6. Pre-Spark runtime checkpoint

### 6.1. Свежесть

Предварительный checkpoint PHASE B можно переиспользовать только если одновременно не изменились:

- mutable paths по timestamps/hashes/counters;
- DB identity/schema/integrity;
- Docker volume/container mapping;
- profile/runtime configuration;
- production commit;
- пользовательские данные после checkpoint.

При любом сомнении создай новый checkpoint непосредственно перед PHASE C.

### 6.2. Свободное место

До копирования оцени размер. Минимальный запас:

```text
estimated backup size × 1.25 + максимальный разрешённый рост задач + 10 GiB
```

Не заполняй системный или Docker-диск ради backup. При недостатке места блокируй state-mutating задачи.

### 6.3. Консистентность SQLite

Запрещена простая копия работающей SQLite DB без доказательства consistency.

Используй один из безопасных способов:

1. штатно остановить только JARVIS и убедиться, что writers завершились;
2. использовать Python `sqlite3.Connection.backup()`/SQLite backup API;
3. использовать документированный project-specific backup mechanism.

После backup обязательно:

- открыть **копию**, а не оригинал;
- выполнить `PRAGMA integrity_check`;
- зафиксировать schema/user_version;
- проверить ключевые table counts/invariants;
- сохранить SHA-256;
- доказать, что backup читается из временного restore root.

Не выполняй опасный checkpoint/truncate/repair оригинала только ради резервной копии без отдельного подтверждённого требования.

### 6.4. Файлы и каталоги

Копируй только mutable valuable state. При recursive copy:

- не следуй junction/symlink/reparse points вслепую;
- исключи models, Docker images, rebuildable caches и большие evidence;
- сохраняй timestamps/attributes where practical;
- фиксируй skipped/locked files;
- проверяй counts, sizes и hashes критических файлов;
- не объявляй backup полным при partial copy.

Uploaded/user files должны входить в backup, если хотя бы одна READY-задача может их изменить или удалить.

### 6.5. Docker volumes

Для каждого named volume определи, содержит ли он mutable critical data.

- Если volume только cache/rebuildable — задокументируй exclusion.
- Если critical — останови только связанные JARVIS writers и экспортируй volume read-only безопасным методом без pull нового образа.
- Если безопасного локального export нет — задачи, способные изменить volume, получают `BLOCKED_BY_SAFETY`.
- Не удаляй и не переименовывай рабочий volume для проверки backup.

Сохрани container/volume/network/compose inventory и hashes/metadata доступных exports.

### 6.6. Restore rehearsal

Восстановление проверяется **в отдельный временный root/DB/volume**, никогда поверх рабочей копии.

Минимальные oracles:

- backup manifest сходится;
- critical hashes/counts сходятся;
- SQLite integrity check PASS;
- restored config parses;
- restored files доступны;
- временный smoke не использует production state;
- cleanup rehearsal не затронул рабочую систему.

Без успешной rehearsal persistent-state-mutating задачи остаются заблокированными.

---

## 7. Task-level checkpoint

Перед задачей с `requires_pre_task_snapshot: true`:

1. верни runtime в task-defined known state;
2. создай snapshot только затрагиваемых mutable roots;
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

Тесты corruption/disk-full/permissions выполняются только на task snapshot, synthetic root или test volume. Рабочая БД/volume не используется как мишень fault injection.

---

## 8. Что делать при неудаче текущей задачи

Если patch ещё не committed и validation провалилась:

1. останови только процессы, созданные этой задачей;
2. сохрани evidence и task report вне изменяемых production files;
3. в remediation worktree восстанови **только явные allowed files** из task tag через `git restore --source=<TAG> -- <explicit paths>`;
4. не используй `git reset --hard`;
5. если task меняла runtime state, восстанови task snapshot и проверь rollback oracles;
6. пометь task `BLOCKED_VALIDATION_FAILED` либо точным статусом;
7. не оставляй полусломанный patch для следующей задачи.

Если задача уже committed, а batch validation обнаружила регрессию:

- не переписывай историю;
- установи responsible commit;
- создай `git revert <SHA>` как отдельный локальный commit, если откат доказуемо безопасен;
- либо создай узкую follow-up задачу;
- после revert повтори regression и runtime smoke;
- зафиксируй связь task → bad commit → revert commit.

Если безопасный автоматический restore не доказан, не экспериментируй. Установи `NEEDS_HUMAN_RESTORE`, останови дальнейшие state-mutating задачи и выдай точный runbook.

---

## 9. Stop conditions — немедленная аварийная остановка

Останови текущую и последующие задачи при любом из условий:

- текущий Git root/branch не соответствует worktree identity;
- обнаружены изменения в `D:\jarvis-gpt`;
- найден чужой или необъяснимый dirty diff;
- task выходит за `allowed_files`/mutable roots;
- команда требует broad delete/prune/reset/clean/factory reset;
- backup или hash verification перестали проходить;
- DB integrity check не PASS;
- restore rehearsal не PASS;
- затронут посторонний process/container/volume/network;
- свободное место падает ниже рассчитанного floor;
- runtime/resource growth превышает task budget;
- требуется скачать/обновить модель, image или dependency вопреки scope;
- реальная команда опаснее описанной в task;
- найден новый critical security/data-loss defect;
- cleanup не возвращает known-good state;
- невозможно определить, какие данные были изменены.

Действия:

1. прекратить новые stimuli;
2. не маскировать ошибку и не продолжать «ради прогресса»;
3. сохранить `INCIDENT_LOG.md`;
4. перевести safety state в `ABORTED` или `NEEDS_HUMAN_RESTORE`;
5. восстановить только то, для чего существует проверенный checkpoint;
6. выполнить безопасный integrity/smoke check;
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

- `taskkill`/`Stop-Process` по широкому имени без PID ownership;
- остановка всех Docker containers;
- остановка/удаление чужой WSL distribution;
- global firewall/DNS mutation без обратимого scoped rule и exact cleanup;
- бесконтрольный fuzz/soak;
- unlimited logs/dumps;
- kill процесса, не созданного или не подтверждённого как JARVIS-owned.

---

## 11. Финальный результат PHASE C не сливается автоматически

PHASE C заканчивается состоянием:

```text
CANDIDATE_FOR_REVIEW
```

а не `MERGED` или «готово в main».

Обязательные финальные свойства:

- все commits находятся только в `spark-remediation/<RUN_ID>`;
- `main`/исходная пользовательская ветка не переключалась и не менялась;
- push не выполнялся;
- merge не выполнялся;
- worktree, branch, tags, bundle и backups сохранены;
- финальная validation выполнена;
- runtime возвращён к документированному состоянию;
- `spark/REMEDIATION_SUMMARY.md` содержит task → commit → rollback mapping;
- `spark/POST_FIX_VALIDATION.md` содержит реальные проверки;
- `spark/ROLLBACK_INDEX.md` содержит команды полного и частичного отката;
- unresolved/blockers перечислены честно.

Пользователь получает ветку-кандидат и решает, просматривать ли её, отправлять на дополнительный review или сливать.

---

## 12. `ROLLBACK_INDEX.md`

Минимально опиши:

### Отказ от всей работы Spark

```powershell
# Ничего не сливать. Канонический checkout остаётся на исходной ветке.
git -C D:\jarvis-gpt status --short --branch
```

Remediation branch/worktree просто сохраняются для анализа или позже удаляются человеком.

### Откат одной уже committed задачи внутри remediation branch

```powershell
git revert <TASK_COMMIT_SHA>
```

После revert обязательны task regression, соседний suite и runtime smoke.

### Восстановление Git при повреждении `.git`

Опиши проверенный clone/fetch из:

```text
D:\jarvis\audit-backups\<RUN_ID>\git\jarvis-gpt-pre-spark.bundle
```

Не выполняй destructive restore автоматически поверх существующего репозитория.

### Восстановление runtime state

Укажи:

- требуемый stopped state;
- точный backup path;
- exact restore commands;
- какие working paths сначала переименовываются/карантинируются, а не удаляются;
- DB/file/volume integrity oracles;
- normal smoke;
- способ возврата runtime в первоначальный профиль/state.

Никакой restore-команды с wildcard broad delete.

---

## 13. Consistency gate перед `spark/safety/READY`

Проверка должна отклонить READY, если:

- нет отдельного worktree plan;
- remediation branch совпадает с `main`/`master`/default branch;
- нет audited source SHA;
- нет Git bundle plan и verification oracle;
- не классифицирована хотя бы одна Spark task;
- state-mutating task не имеет snapshot/restore fields;
- mutable critical object отсутствует в mutation map;
- нет свободного места;
- backup partial/unverified;
- restore rehearsal failed/not run для relevant tasks;
- обычный `spark/READY` существует без safety READY;
- cleanup/known-good state PHASE B не доказан;
- есть high-risk task без human gate или safe isolation;
- есть broken paths/unknown IDs.

Только после PASS:

1. обнови `SAFETY_STATE.json` до `READY`;
2. создай пустой `spark/safety/READY`;
3. затем разрешено создать/сохранить обычный `spark/READY`.

---

## 14. Главное правило

При конфликте между скоростью исправлений и возможностью доказуемого отката всегда выбирай откат.

Если безопасное исполнение конкретной задачи невозможно, правильный результат — `BLOCKED_BY_SAFETY`, а не риск для `D:\jarvis-gpt`, пользовательской БД, файлов, моделей, Docker volumes или всей рабочей машины.
