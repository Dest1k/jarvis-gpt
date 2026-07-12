# JARVIS — КАНОНИЧЕСКАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ АУДИТА И ИСПРАВЛЕНИЙ

Используй ровно три launcher-prompt в таком порядке.

## PHASE A — облачный аудит репозитория

```text
docs/audit/01_JARVIS_REPOSITORY_ONLY_AUDIT_PROMPT.md
```

Запускается в Work / облачном checkout через Sol Ultra. Не требует доступа к живой Windows-машине.

Результат должен попасть обратно в локальный `D:\jarvis-gpt` вместе с `.audit/LATEST_STATIC_RUN.txt` и run-каталогом.

## PHASE B — живой аудит с подготовкой безопасного remediation-контура

```text
docs/audit/02_SAFE_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md
```

Запускается через локальный Codex Sol Ultra на целевой машине.

Этот launcher заставляет Sol прочитать одновременно:

- полный core-аудит `02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md`;
- обязательный safety/rollback protocol `04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md`.

Не запускай core-файл `02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md` напрямую: он остаётся подробной спецификацией тестовой кампании, а safety launcher добавляет worktree, branch, bundle, runtime checkpoint и restore gate.

## PHASE C — исправления через Spark

```text
docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
```

Запускается через локальный Codex Spark только после завершения PHASE B.

Не запускай `03_JARVIS_SPARK_REMEDIATION_PROMPT.md` напрямую. Он является core task-loop, а safe launcher добавляет обязательную техническую изоляцию и откат.

## Обязательные барьеры перед Spark

Spark не должен начинать, пока одновременно не существуют:

```text
.audit/LATEST_COMPLETE_RUN.txt
.audit/runs/<RUN_ID>/spark/READY
.audit/runs/<RUN_ID>/spark/safety/READY
```

И пока не доказаны:

- отдельный worktree `D:\jarvis-gpt-worktrees\spark-<RUN_ID>`;
- отдельная ветка `spark-remediation/<RUN_ID>`;
- tags `pre-spark-source-<RUN_ID>` и `pre-spark-<RUN_ID>`;
- verified Git bundle;
- verified runtime checkpoint для всех mutable state, которых могут коснуться READY-задачи;
- restore rehearsal;
- known-good runtime state;
- safety consistency gate.

## Что Spark не делает

- не правит `D:\jarvis-gpt`;
- не коммитит в main/default branch;
- не делает push;
- не делает merge;
- не переписывает историю;
- не удаляет worktree, backups, tags или evidence;
- не выполняет state-mutating task без verified snapshot/restore.

Финальный результат Spark — локальная ветка-кандидат `CANDIDATE_FOR_REVIEW`, а не автоматическое изменение main.
