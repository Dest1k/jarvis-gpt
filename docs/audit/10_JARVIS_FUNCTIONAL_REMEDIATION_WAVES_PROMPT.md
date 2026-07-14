# JARVIS — ФУНКЦИОНАЛЬНАЯ РЕМЕДИАЦИЯ ПРОВЕРЕННЫМИ ВОЛНАМИ

Этот протокол заменяет `07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md` для
функционального run `20260713T002206Z_686424795712` и служит шаблоном для
последующих кампаний со статусом `COMPLETE_WITH_BLOCKERS`. Старый протокол
`07` не запускай.

Цель одного запуска — выполнить ровно одну явно выбранную remediation wave,
получить по одному локальному commit на задачу и остановиться для review. Этот
протокол не разрешает продолжать функциональный аудит, подменять readiness
markers, автоматически переходить к следующей волне, выполнять push/merge или
обещать исправление 31B-профилей.

Все сообщения пользователю пиши по-русски. Краткие подробности сохраняй в
task/wave reports. Исходные `.audit/**` — immutable evidence завершённой
кампании.

## 1. Обязательные явные параметры

До любых изменений оператор обязан явно задать:

```text
RUN_ID=20260713T002206Z_686424795712
TARGET_WAVE=<WAVE-0|WAVE-1|WAVE-2>
REVIEWED_FOUNDATION_COMMIT=<полный 40-символьный SHA>
REVIEWED_INPUT_COMMIT=<полный 40-символьный SHA exact start HEAD этой wave>
```

Не выбирай `TARGET_WAVE` по умолчанию, по позиции в очереди или по истории
предыдущей сессии. Любое другое значение — blocker
`BLOCKED_BY_INVALID_TARGET_WAVE`. Оба commit parameters должны быть exact
40-символьными lowercase SHA; branch name, tag, short SHA или symbolic ref
недопустимы.

Для этого run используй:

```text
FUNCTIONAL_RUN=.audit/runs/20260713T002206Z_686424795712/functional
OVERLAY_ROOT=docs/assurance/remediation/20260713T002206Z_686424795712
WAVE_PLAN=docs/assurance/remediation/20260713T002206Z_686424795712/WAVES.yml
```

`SPARK-0013` не входит в `WAVE-0..2`. `PROFILE-SAFETY` и
`PROFILE-RESEARCH` — отдельный product-decision gate из
`PROFILE_DECISION.md`, а не допустимые значения `TARGET_WAVE`. Не выполняй
их этим протоколом без отдельного явного решения пользователя и отдельного
reviewed execution protocol.

## 2. Fail-closed preflight

Начинай в основном checkout `D:\jarvis-gpt` только с read-only Git/evidence
проверок и действий из rollback protocol. Прочитай:

```text
docs/audit/08_JARVIS_FUNCTIONAL_REMEDIATION_ROLLBACK_PROTOCOL.md
<FUNCTIONAL_RUN>/FUNCTIONAL_STATE.json
<FUNCTIONAL_RUN>/FUNCTIONAL_ASSURANCE_STATEMENT.md
<FUNCTIONAL_RUN>/FUNCTIONAL_FINDINGS_INDEX.md
<FUNCTIONAL_RUN>/spark/QUEUE.csv
<FUNCTIONAL_RUN>/spark/TASK_SCHEMA.md
<OVERLAY_ROOT>/WAVES.md
<OVERLAY_ROOT>/WAVES.yml
<OVERLAY_ROOT>/PROFILE_DECISION.md
<OVERLAY_ROOT>/ACCEPTANCE_MAP.md
repository instructions
```

Запуск разрешён только при одновременном выполнении условий:

```text
FUNCTIONAL_STATE.status in {COMPLETE_WITH_BLOCKERS, COMPLETE}
FUNCTIONAL_STATE.progress_percent == 100
FUNCTIONAL_STATE.markers.spark_ready == true
<FUNCTIONAL_RUN>/spark/READY существует
SCENARIO_QUEUE.csv не содержит NOT_RUN
каждый FAIL в RESULTS.csv и OPERATOR_ACCEPTANCE_RESULTS.csv связан с finding
каждый finding из FAIL связан ровно с существующей Spark task
REVIEWED_FOUNDATION_COMMIT существует локально и явно подтверждён как reviewed
REVIEWED_INPUT_COMMIT существует локально и точно связан с independent review
WAVES.yml согласован с immutable QUEUE.csv и task files
tracked diff и index основного checkout чисты
```

`functional/READY` не является precondition. При подтверждённых дефектах его
отсутствие ожидаемо. Не создавай, не копируй и не имитируй этот marker: он
может появиться только после отдельной успешной post-fix acceptance campaign.
Старое поле `operator_ready` не трактуй как product readiness; в отчётах
используй термины `operator_suite_complete` и
`remediation_input_ready`.

Проверь, что `REVIEWED_FOUNDATION_COMMIT` — полный SHA commit, содержит
permanent QA harness и этот overlay. До создания worktree exact
`REVIEWED_INPUT_COMMIT^{commit}` обязан разрешиться в тот же переданный SHA;
ancestor/contains/branch-name проверки недостаточны. Для `WAVE-0`
`REVIEWED_INPUT_COMMIT == REVIEWED_FOUNDATION_COMMIT` и это exact independently
reviewed foundation attestation commit. Для `WAVE-1/2` input определяется
только approved review record предыдущей wave. Словесного утверждения
«reviewed» без указанного commit недостаточно. При любом несовпадении ничего
не изменяй и верни `BLOCKED_BY_UNREVIEWED_START_HEAD`.

До запуска wave выполни offline source-integrity verifier. Он обязан получить
ровно `4/4` Git-blob source pins и `17/17` task/finding/path mappings из exact
source commit; checkout EOL и mutable checkout files не являются источником:

```powershell
py -3.11 -m qa.cli validate-overlay-sources `
  --repository-root <repository-root> `
  --overlay docs/assurance/remediation/20260713T002206Z_686424795712/WAVES.yml `
  --expected-source-commit 5aae9855f0779c746ec9287c2ec8917637fedb36 `
  --git-executable <trusted-absolute-git-executable>
```

Любой missing/stale commit, path, blob OID, SHA-256 или mapping —
`BLOCKED_BY_OVERLAY_SOURCE_INTEGRITY`.
Verifier разбирает весь overlay как canonical dependency-free YAML subset и
связывает mappings только с фактическими `waves`/`product_decision_gate`.
Block scalar, shadow/duplicate section и alternate noncanonical representation
являются `BLOCKED_BY_OVERLAY_SOURCE_INTEGRITY`.

Никогда не открывай локальные raw evidence с реальными credentials. Используй
только committed sanitized evidence и disposable canary values. Не выполняй
broad secret/env dump и не запускай `docker compose config` без redaction.

## 3. Изоляция, страховка и цепочка review

Следуй `08_JARVIS_FUNCTIONAL_REMEDIATION_ROLLBACK_PROTOCOL.md`. Основной
checkout не изменяй. Для `WAVE-0` создай от точного `REVIEWED_INPUT_COMMIT`,
равного `REVIEWED_FOUNDATION_COMMIT`:

```text
branch:   spark-functional/20260713T002206Z_686424795712
worktree: D:\jarvis-gpt-worktrees\functional-20260713T002206Z_686424795712
backup:   D:\jarvis\audit-backups\20260713T002206Z_686424795712\functional
```

До первого изменения создай требуемые protocol `08` tags, Git bundle,
`git bundle verify` result, SHA-256 и manifests. До создания нового worktree
проверь exact resolution `REVIEWED_INPUT_COMMIT^{commit}`; worktree создавай
явно из этого SHA, а не из содержимого основного каталога. Сразу после создания
или открытия existing worktree и повторно непосредственно перед первым
изменением этой wave требуется exact equality:

```text
git rev-parse HEAD == REVIEWED_INPUT_COMMIT
```

Также выполни machine gate:

```powershell
py -3.11 -m qa.cli verify-reviewed-input `
  --repository-root <wave-worktree> `
  --reviewed-input-commit <REVIEWED_INPUT_COMMIT> `
  --git-executable <trusted-absolute-git-executable>
```

Gate отклоняет staged, tracked, untracked и ignored state, nondefault index
flags (`assume-unchanged`/`skip-worktree`) и любое несовпадение index/worktree
bytes с exact reviewed tree. Проверка выполняет два независимых worktree hash
pass, запрещает external Git content filters и отключает repository-controlled
fsmonitor/hooks. Такие состояния дают `BLOCKED_BY_UNCOMMITTED_START_STATE`.

Если branch/worktree уже существуют, не пересоздавай их: проверь root, branch,
exact HEAD и external manifests. Ancestry, descendant/contains или наличие
reviewed commit в history не заменяют equality. Любой extra local commit или
несовпадение — `BLOCKED_BY_UNREVIEWED_START_HEAD`.

`WAVE-1` и `WAVE-2` продолжают ту же изолированную commit chain, но только
после отдельного review предыдущей волны:

```text
TARGET_WAVE=WAVE-1 требует committed reviews/WAVE-0.yml:
  status=APPROVED и reviewed_head=<точный финальный HEAD WAVE-0>
  REVIEWED_INPUT_COMMIT == reviewed_head

TARGET_WAVE=WAVE-2 требует committed reviews/WAVE-1.yml:
  status=APPROVED и reviewed_head=<точный финальный HEAD WAVE-1>
  REVIEWED_INPUT_COMMIT == reviewed_head
```

Review record хранится под `<OVERLAY_ROOT>/reviews/`, называет reviewer,
`reviewed_head`, timestamp и результаты проверки. Его нельзя создавать или
одобрять автоматически в том же запуске, который выполнил wave. Отсутствующий,
неполный или не совпадающий review — `BLOCKED_BY_INTER_WAVE_REVIEW`. Даже
потомок approved `reviewed_head` блокируется как unreviewed start HEAD. Один
запуск по-прежнему выполняет только одну wave и никогда автоматически не
переходит к следующей.

### External content manifests для `.audit/**`

Path list, count, `git diff`, tree OID и tracked-only comparison недостаточны:
они не защищают bytes untracked evidence. До wave создай во внешнем backup root
отдельные manifests для `.audit/**` основного checkout и wave worktree. После
каждого snapshot сохрани выданный SHA-256 как trusted out-of-band anchor в
checkpoint metadata. Backup root обязан существовать, быть disjoint от
repository и не должен быть symlink/reparse path.

```powershell
py -3.11 -m qa.cli audit-manifest-create `
  --repository-root <checkout-or-worktree> `
  --backup-root <external-backup-root> `
  --output-name <unique-snapshot-name.json> `
  --git-executable <trusted-absolute-git-executable>

py -3.11 -m qa.cli audit-manifest-compare `
  --repository-root <checkout-or-worktree> `
  --backup-root <external-backup-root> `
  --before-name <baseline-name.json> `
  --after-name <current-name.json> `
  --expected-before-sha256 <retained-baseline-sha256> `
  --expected-after-sha256 <retained-current-sha256> `
  --result-name <unique-comparison-name.json> `
  --git-executable <trusted-absolute-git-executable>
```

Snapshot перечисляет всё `.audit` tree независимо от tracked/untracked status:
canonical relative path, file type, size и raw-file SHA-256. Directories и
symlink/reparse metadata фиксируются отдельно; ссылки не следуются. Special,
inaccessible, raced, oversized, unsafe или неполные entries являются blocker,
а не пропускаются. Manifest содержит только metadata, никогда evidence bytes;
он проходит redaction/validation и создаётся exclusive только во внешнем root.
Каждый regular file проходит два независимых bounded digest pass; mismatch,
включая same-size mutation с восстановленными timestamps, является race blocker.
Появление, исчезновение, изменение type, size или hash, mismatch retained
manifest SHA либо repository-local output дают
`BLOCKED_BY_AUDIT_CONTENT_MANIFEST`.

Запрещены push, merge, rebase, squash, force-update, `git reset --hard`,
aggressive clean и auto-stash. Не переписывай плохой commit: используй узкий
revert commit после review либо отдельную явно одобренную follow-up task.

## 4. Единственный допустимый порядок

Состав задач берётся только из `WAVES.yml` и обязан совпадать с этим списком:

```text
WAVE-0:
  SPARK-0017
  SPARK-0016
  SPARK-0006
  SPARK-0009
  SPARK-0015
  SPARK-0011

WAVE-1:
  SPARK-0014
  SPARK-0007
  SPARK-0003
  SPARK-0008

WAVE-2:
  SPARK-0002
  SPARK-0004
  SPARK-0005
  SPARK-0012
  SPARK-0001
  SPARK-0010
```

Не добавляй, не исключай и не переставляй задачи. Работай строго по одной.
Следующая задача разрешена только после terminal report и одного локального
commit текущей. Любой blocker, невоспроизведённый исходный FAIL, safety
failure, scope drift или непроходящая validation останавливает всю текущую
wave до review; не перескакивай к следующей задаче.

Immutable `<FUNCTIONAL_RUN>/spark/QUEUE.csv` и task files не редактируй.
Execution state и reports создавай под:

```text
<OVERLAY_ROOT>/execution/<TARGET_WAVE>/
  MANIFEST.yml
  tasks/<TASK_ID>.md
  WAVE_VALIDATION.md
```

## 5. Цикл одной задачи

Для текущей задачи:

1. Проверь Git root, branch, HEAD, status, upstream absence и task order. Перед
   первым изменением первой task wave повтори exact
   `HEAD == REVIEWED_INPUT_COMMIT`; extra commit останавливает запуск.
2. Создай task checkpoint/tag по protocol `08` и запиши pre-task HEAD. Создай
   новый external `.audit` snapshot, сохрани его SHA-256 и сравни с anchored
   pre-wave manifest до чтения/изменения task state.
3. Прочитай только её immutable task file, source finding, перечисленные
   context files и необходимые committed sanitized evidence.
4. Классифицируй runtime impact:
   `code_only`, `runtime_read_only`, `runtime_temporary`,
   `persistent_state` или `container_configuration`.
5. Для state/config changes выполни backup, integrity и trial-restore checks
   protocol `08` до изменения. Непроверяемый restore даёт
   `BLOCKED_BY_SAFETY`.
6. Безопасно воспроизведи исходный FAIL. Если он не воспроизводится, не делай
   speculative patch; восстанови task-owned changes по protocol `08`, создай
   report-only blocker commit для этой task и останови wave.
7. Добавь focused regression test, проверяющий контракт, а не случайную
   дословную фразу модели.
8. Сделай минимальный patch только в `Allowed files` task file. Новые test
   helpers должны быть узкими и обоснованными в report.
9. Запусти focused test, task exact validation commands и один directly
   affected neighboring suite. Полный live model suite здесь не запускай.
10. Выполни bounded post-fix replay через permanent QA harness и повтори
    harmless acceptance journey из task file нужное число раз. Для live
    проверок используй только изолированный runtime и canary credentials.
11. Сопоставь claimed actions с фактическими state/artifacts, проверь
    stream/final integrity, cleanup и normal smoke.
12. После reproduction, patch, tests и cleanup отдельно проверь scope,
    `git diff` и `git diff --check`; создай новый external `.audit` snapshot и
    anchored comparison. Git-only проверка не доказывает неизменность
    untracked evidence.
13. Заполни immutable-by-commit task report: finding, before/after evidence,
    exact commands/results, cleanup, rollback, changed paths и residual risks.
14. Создай ровно один локальный commit для этой задачи. Успешный commit
    включает patch, regression tests и task report. Blocked task после входа
    в task loop получает только report-only commit без production/test patch.
    Не включай файлы другой задачи. Для последней task wave отложи этот
    единственный commit до batch validation из раздела 7.

Не используй production credentials, не загружай модели/containers/repos/
binaries и не устанавливай dependencies. Не исполняй внешний код. Не меняй
`D:\jarvis` вне явно изолированных, task-owned временных объектов и проверенных
копий protocol `08`.

## 6. Permanent QA и правила verdict

Task-specific tests обязательны. Дополнительно используй команды foundation:

```powershell
py -3.11 -m qa.cli validate-suite qa\suites\operator_core
py -3.11 -m qa.cli validate-evidence <committed-sanitized-evidence.jsonl> --expected-manifest-sha256 <retained-sha256>
py -3.11 -m qa.cli replay <committed-sanitized-evidence.jsonl> --expected-manifest-sha256 <retained-sha256> --output <replay.json>
py -3.11 -m qa.cli build-review-packets <committed-sanitized-evidence.jsonl> --expected-manifest-sha256 <retained-sha256> --output-dir <packets>
py -3.11 -m qa.cli adjudicate <review-1.json> <review-2.json> --replay <replay.json> --evidence <committed-sanitized-evidence.jsonl> --context-anchor-1 <sha256> --context-anchor-2 <sha256> --review-anchor-1 <sha256> --review-anchor-2 <sha256> --expected-manifest-sha256 <retained-sha256>
```

Используй bounded subset, связанный с текущей task; не подменяй им task exact
commands. Reviewer packets содержат только sanitized request, expected
contract, actual output и bounded evidence. Reviews выполняются раздельно,
имеют explicit independence level и не видят verdict друг друга.

Детерминированный `FAIL` нельзя повысить до `PASS` semantic review.
Disagreement без детерминированного решения даёт `INCONCLUSIVE`. Отсутствие
evidence никогда не даёт `PASS`. Несовпадение process exit code и
machine-readable result — `FAIL`.

## 7. Batch validation и обязательная остановка

После последней задачи `TARGET_WAVE`:

1. Проверь task-to-commit mapping и точный порядок commits.
2. Запусти task-specific deterministic suites для всех задач wave один раз.
3. Выполни bounded permanent-QA replay и validators по
   `ACCEPTANCE_MAP.md`.
4. Для semantic cases создай два раздельных review output и adjudication.
5. Проверь отсутствие tool/transport leaks, empty/duplicate finals, false
   success, canary secrets, state/artifact mismatch и cross-runtime bleed.
6. Проверь cleanup, rollback assets, clean worktree и `git diff --check`.
7. Создай final external `.audit` manifests wave worktree и основного checkout.
   С retained SHA anchors сравни каждый с соответствующим pre-wave manifest;
   path/type/size/hash differences должны быть нулевыми. Дополнительно проверь
   tracked Git tree, но не используй его вместо content manifests.
8. Запиши `WAVE_VALIDATION.md`. Для последней задачи wave выполни batch
   validation до её единственного commit и включи этот report в тот commit.
   Если batch validation не PASS, восстанови незакоммиченный patch последней
   task по её checkpoint, сохрани blocker report и сделай только её
   report-only commit; candidate state запрещён.

Не запускай следующую wave. Не создавай `functional/READY`. Не выполняй push
или merge. Заверши ровно одним состоянием:

```text
WAVE_0_CANDIDATE_FOR_REVIEW
WAVE_1_CANDIDATE_FOR_REVIEW
WAVE_2_CANDIDATE_FOR_REVIEW
```

Если любой обязательный пункт не выполнен, верни точный
`BLOCKED_BY_<REASON>` и не объявляй wave кандидатом.

## 8. Финальный ответ одного запуска

Кратко укажи:

- run ID и explicit target wave;
- worktree, branch, reviewed foundation SHA, exact reviewed input SHA и final HEAD;
- task-to-commit mapping в точном порядке;
- test/replay/review counts;
- rollback/cleanup status;
- подтверждение неизменности основного checkout и всего tracked/untracked
  `.audit/**` по anchored external content manifests;
- отсутствие push/merge и `functional/READY`;
- residual blockers;
- итоговое `WAVE_N_CANDIDATE_FOR_REVIEW` либо точный blocker.
