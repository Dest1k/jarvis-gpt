# JARVIS — КАНОНИЧЕСКИЙ ЗАПУСК PHASE B НА ЖИВОЙ МАШИНЕ

Ты выполняешь PHASE B двухкомпонентного доказательного аудита JARVIS через Codex Sol Ultra на принадлежащей пользователю Windows-машине.

Этот файл является **единственным каноническим launcher-prompt для PHASE B**. Он объединяет полный план проверки качества, обязательный операторский acceptance-протокол и контур изоляции/отката.

Цель работы — функциональная корректность, надёжность, целостность данных, восстановимость, производительность, качество интерфейса и пригодность JARVIS к обычной работе оператора. Недостаточно доказать, что endpoints и tools технически отвечают: JARVIS должен понимать задачу, соблюдать инструкции, доводить доступное действие до полезного результата, честно сообщать блокеры и не показывать пользователю внутренний JSON/protocol вместо ответа.

Не выполняй активные наступательные проверки, не воздействуй на внешние системы и не формируй инструкции по нарушению ограничений. Все проверки границ делай только на безвредных синтетических примерах, loopback fixtures, временных каталогах и копиях состояния.

Все сообщения пользователю пиши только на русском. Подробности сохраняй в `.audit/**`; в чате сообщай процент готовности, counts, paths, blockers и безопасные следующие действия. Плановые отчёты — на 50% и 90% готовности. Раньше сообщай только о реальном blocker или необходимости решения пользователя.

---

## 1. Канонические каталоги

```text
D:\jarvis-gpt   — Git-репозиторий и audit artifacts
D:\jarvis       — модели, Docker/runtime-данные, backups и тяжёлые evidence
```

Начни:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
git status --short --branch
git rev-parse HEAD
```

Ожидаемый Git root — `D:/jarvis-gpt`. `D:\jarvis` не является репозиторием.

Не выполняй `reset --hard`, aggressive clean, auto-stash или присвоение пользовательских изменений.

---

## 2. Проверка актуальности документов без потери handoff

До начала аудита:

1. сохрани список существующих untracked/modified файлов;
2. отдельно зафиксируй наличие `.audit/LATEST_STATIC_RUN.txt` и run-каталога;
3. выполни `git fetch`;
4. допускается только обычный fast-forward текущей ветки, если Git доказывает отсутствие конфликтующих tracked-изменений;
5. существующие untracked `.audit/**` не удаляй и не перезаписывай молча;
6. если fast-forward невозможен, остановись с точным blocker — не используй reset/rebase/stash;
7. после синхронизации повторно проверь Git root, status, prompt-файлы и handoff.

Изменения только в `docs/audit/**` не считаются production source drift, но новая редакция каждого обязательного prompt/protocol должна быть прочитана полностью.

Если PHASE A artifacts находятся в отдельной audit-ветке/commit и ещё не присутствуют локально, получи их безопасным способом, сохранив production branch и пользовательские изменения. Разрешено восстановить только `.audit/**` из известного audit commit после проверки commit SHA и путей. Не переключай ветку вслепую и не копируй production-файлы из audit-ветки.

---

## 3. Обязательные документы

Полностью прочитай и исполняй совместно:

1. `docs/audit/02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md` — core-план проверки качества;
2. `docs/audit/05_JARVIS_OPERATOR_ACCEPTANCE_AND_RESPONSE_QUALITY_PROTOCOL.md` — следование инструкциям, полезность результата и целостность пользовательских ответов;
3. `docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md` — изоляция, backups и rollback;
4. `.audit/LATEST_STATIC_RUN.txt` и PHASE A handoff;
5. актуальные repository instructions (`AGENTS.md`, `CODEX.md`, `CLAUDE.md` и локальные эквиваленты).

Все три audit-документа выполняются целиком. При конфликте:

- operator acceptance protocol имеет приоритет в вопросах ordinary user journeys, instruction following, response integrity, false success, internal protocol leakage и критериев пользовательской готовности;
- rollback protocol имеет приоритет в Git/worktree/backups/runtime/Docker/restore/stop conditions;
- repository instructions имеют приоритет в локальных правилах, если не ослабляют acceptance и изоляцию;
- выбери более безопасное и более строго проверяемое поведение и запиши решение.

---

## 4. Возобновление после прежней попытки

Если предыдущая PHASE B оборвалась, была остановлена интерфейсом или оставила background processes:

1. не удаляй существующие artifacts;
2. проверь `PIPELINE_STATE.json`, `AUDIT_STATE.md`, `LIVE_SCENARIO_QUEUE.csv` и evidence manifest;
3. проверь Git status, process/container/port/runtime state;
4. останови только явно принадлежащие прежней попытке fixtures/processes;
5. верни документированное known-good состояние;
6. сохрани `RESUME_NOTE.md` с точкой остановки и проверенными artifacts;
7. не засчитывай незавершённый chat-ответ как PASS;
8. продолжи с первой scenario, у которой нет полного evidence и конечного статуса;
9. не повторяй дорогие PASS-сценарии, если их evidence, source и preconditions доказуемо неизменны;
10. отдельно проверь, не остались ли незавершённые operator acceptance transcripts или streaming captures: неполная запись не является PASS.

Если невозможно определить, что было изменено прежней попыткой, установи `BLOCKED_BY_SAFETY` и остановись до решения пользователя.

---

## 4A. Главный операторский acceptance gate

Выполни `05_JARVIS_OPERATOR_ACCEPTANCE_AND_RESPONSE_QUALITY_PROTOCOL.md` как отдельный обязательный workstream, а не как необязательный smoke.

До завершения PHASE B должны существовать и быть заполнены:

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

Обязательно проверь через реальные end-to-end user journeys:

- выполнение обычных задач, а не только работу endpoint/tool;
- точное соблюдение языка, формата, scope, запретов и критериев готовности;
- многоходовый контекст и clarification;
- документы, локальное состояние, missions, memory, web/browser и другие фактические пользовательские поверхности;
- честный failure вместо «готово» без результата;
- отсутствие ответов вида «посмотрите здесь/сделайте сами» при доступной возможности выполнить задачу;
- отсутствие raw tool-call JSON, schemas, transport frames, prompt fragments, traceback и иных внутренних данных в обычном чате;
- совпадение заявленных действий с UI/API/storage/tool evidence;
- streaming reconstruction, отсутствие duplicate/truncated final;
- полезность итогового результата для оператора.

Core technical suites не заменяют этот gate. Если real-model operator suite не выполнен, не заявляй готовность JARVIS к обычной работе и не создавай Spark READY markers.

---

## 5. Обязательная цель PHASE B

После полного live-аудита ты обязан не только создать Spark queue, но и подготовить технически изолированный, проверяемо откатываемый remediation-контур.

До создания обычного marker:

```text
.audit/runs/<RUN_ID>/spark/READY
```

выполни пункты ниже.

### 5.1. Классифицируй каждую Spark task

Заполни:

- `mutation_class`;
- `mutable_roots`;
- `requires_stack_stop`;
- `requires_pre_task_snapshot`;
- `requires_restore_rehearsal`;
- `human_gate_required`;
- resource/process/network budgets;
- rollback checkpoint, commands и oracles.

Задача с неизвестным mutation scope не может быть READY.

Каждый подтверждённый operator defect — ignored instruction, vague handoff, false success, missing artifact, internal protocol leak, context loss, duplicate/truncated final или tool-state mismatch — получает отдельную atomic task либо точное `NO_CODE_CHANGE/BLOCKED` rationale. Для неё обязательны sanitized transcript, deterministic validators и post-fix operator journey.

### 5.2. Создай safety artifacts

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

### 5.3. Верни систему в known-good state

После всех recovery/resource/long-run checks:

1. выполни cleanup;
2. верни исходный или явно документированный profile/runtime state;
3. запусти normal smoke;
4. проверь health, ports, containers, processes, GPU release и DB integrity;
5. сохрани baseline для Spark.

### 5.4. Подготовь отдельный remediation worktree

Не меняй production-код в `D:\jarvis-gpt`.

Создай:

```text
D:\jarvis-gpt-worktrees\spark-<RUN_ID>
spark-remediation/<RUN_ID>
```

Если audit artifacts не входят в base commit:

- перенеси только текущий `.audit/runs/<RUN_ID>` и marker-файлы;
- проверь paths/hashes;
- создай audit-only snapshot commit;
- не stage пользовательские или production changes.

Запиши repo/worktree paths, branch, base SHA и pre-remediation SHA в `WORKTREE_IDENTITY.json`.

### 5.5. Создай Git rollback assets

Создай и проверь:

```text
pre-spark-source-<RUN_ID>
pre-spark-<RUN_ID>
D:\jarvis\audit-backups\<RUN_ID>\git\jarvis-gpt-pre-spark.bundle
```

Сохрани SHA-256 и `git bundle verify` evidence. Не выполняй push, merge, rebase или force-update.

### 5.6. Создай verified runtime checkpoint

Для каждого mutable critical object, которого может коснуться READY task:

- создай консистентную резервную копию;
- для SQLite используй backup API или остановленных writers;
- инвентаризируй/экспортируй mutable Docker volumes либо заблокируй связанные tasks;
- проверь hashes/counts/integrity;
- выполни пробное восстановление в temp root;
- проверь свободное место;
- не копируй models/images/rebuildable caches без необходимости.

Если checkpoint или restore rehearsal не доказаны, state-mutating tasks получают `BLOCKED_BY_SAFETY`.

### 5.7. Consistency gate

Только при PASS:

1. установи `SAFETY_STATE.json.state = READY`;
2. создай:

```text
.audit/runs/<RUN_ID>/spark/safety/READY
```

3. затем создай обычный `spark/READY`;
4. создай `.audit/LATEST_COMPLETE_RUN.txt` только при выполнении остальных критериев core-плана и operator acceptance gate.

Обычный `spark/READY` без `spark/safety/READY` запрещён. Оба READY marker также запрещены без обязательных operator acceptance artifacts и real-model coverage.

---

## 6. Что передать Spark

`START_HERE_FOR_SPARK.md` должен требовать запуск через:

```text
docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
```

Он указывает:

- RUN_ID;
- remediation worktree и branch;
- audited/base/pre-spark commits;
- bundle path/hash;
- runtime checkpoint path/status;
- safety state;
- known-good runtime state;
- operator acceptance baseline и paths к reports;
- первую eligible task;
- stop conditions.

Task descriptions должны быть функциональными и нейтральными: contract, harmless reproduction, expected/observed, regression test, scope и rollback. Для user-facing defects добавляй ordinary user journey, sanitized transcript, response-integrity validators и post-fix acceptance command. Не включай длинные operational details по нарушению границ.

---

## 7. Финальный ответ

В дополнение к core-отчёту кратко сообщи по-русски:

- процент готовности 100%;
- run path;
- source/current commit и drift;
- число PASS/FAIL/BLOCKED/INCONCLUSIVE;
- profiles/model mapping;
- количество operator cases/repeats и instruction-following pass rate;
- response-integrity pass rate;
- число internal-protocol-leak, false-success и vague-handoff findings;
- counts findings и READY/BLOCKED tasks;
- точные paths к итоговым documents;
- создан ли отдельный worktree и его path;
- remediation branch и pre-Spark SHA;
- проверен ли Git bundle;
- создан ли runtime checkpoint;
- прошло ли пробное восстановление;
- существуют ли `spark/safety/READY` и `spark/READY`;
- в каком состоянии оставлены JARVIS, Docker и LLM.

Не публикуй в чате raw logs, длинные transcripts, конфиденциальные значения или длинные тестовые входы. Если operator acceptance или safety gate не пройдены, не называй PHASE C готовой к запуску.