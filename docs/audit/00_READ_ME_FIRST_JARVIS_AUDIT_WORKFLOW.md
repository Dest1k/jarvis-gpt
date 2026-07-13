# JARVIS — КАНОНИЧЕСКАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ АУДИТА И ИСПРАВЛЕНИЙ

Используй ровно три launcher-prompt в указанном порядке. Не запускай внутренние core-файлы напрямую.

Все три фазы являются авторизованной проверкой принадлежащего пользователю локального проекта. Формулировки ориентированы на качество, надёжность, целостность данных, восстановимость и нейтральные функциональные проверки. Для границ ввода/доступа используются только harmless synthetic examples, loopback fixtures, temp roots и copied state. Никакого воздействия на внешние системы.

---

## PHASE A — облачный аудит репозитория

```text
docs/audit/01_JARVIS_REPOSITORY_ONLY_AUDIT_PROMPT.md
```

Запускается в Work / облачном checkout через Sol Ultra. Не требует доступа к живой Windows-машине.

Результат должен попасть в локальный `D:\jarvis-gpt` вместе с:

```text
.audit/LATEST_STATIC_RUN.txt
.audit/runs/<RUN_ID>/
```

PHASE A не создаёт Spark READY markers.

---

## Перед PHASE B — синхронизация

Перед новой сессией Sol:

```powershell
Set-Location D:\jarvis-gpt
git status --short --branch
git fetch --all --prune
git pull --ff-only
```

`git pull --ff-only` выполняй только при отсутствии конфликтующих tracked-изменений. Не используй reset/stash/clean для пользовательских файлов.

Убедись, что локально присутствуют:

```text
.audit/LATEST_STATIC_RUN.txt
docs/audit/02_SAFE_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md
docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md
```

Если PHASE A artifacts опубликованы отдельным audit commit/branch, перенеси только `.audit/**` безопасным способом либо следуй точной инструкции PHASE A. Production-код из audit-ветки не подменяй.

---

## PHASE B — живая проверка качества и подготовка remediation-контура

```text
docs/audit/02_SAFE_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md
```

Запускается через локальный Codex Sol Ultra на целевой машине.

Launcher заставляет Sol прочитать совместно:

- `02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md` — полный core-план;
- `04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md` — worktree, branch, bundle, runtime checkpoint и restore gate;
- PHASE A handoff.

Не запускай `02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md` напрямую.

Если прежняя PHASE B оборвалась, запускай новую сессию тем же safe launcher. Он обязан проверить и сохранить существующие artifacts, вернуть known-good state и продолжить с первой незавершённой scenario.

PHASE B должна закончиться двумя markers:

```text
.audit/runs/<RUN_ID>/spark/READY
.audit/runs/<RUN_ID>/spark/safety/READY
```

Без обоих markers PHASE C запрещена.

---

## PHASE C — контролируемые исправления через Spark

```text
docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
```

Запускается через локальный Codex Spark только после завершения PHASE B.

Не запускай `03_JARVIS_SPARK_REMEDIATION_PROMPT.md` напрямую.

Обязательные барьеры:

- отдельный worktree `D:\jarvis-gpt-worktrees\spark-<RUN_ID>`;
- отдельная ветка `spark-remediation/<RUN_ID>`;
- tags `pre-spark-source-<RUN_ID>` и `pre-spark-<RUN_ID>`;
- verified Git bundle;
- verified runtime checkpoint для mutable state;
- пробное восстановление;
- known-good runtime state;
- safety consistency gate.

Spark:

- не правит `D:\jarvis-gpt`;
- не коммитит в default branch;
- не делает push/merge/rebase/squash/force;
- не удаляет worktree, backups, tags или evidence;
- не выполняет state-mutating task без verified snapshot/restore;
- использует только benign test cases и локальную изоляцию.

Финальный результат — локальная ветка-кандидат:

```text
CANDIDATE_FOR_REVIEW
```

а не автоматическое изменение `main`.

---

## Формат запуска

PHASE B:

```text
Прочитай docs/audit/02_SAFE_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md
и полностью выполни содержащуюся в нём задачу.
```

PHASE C:

```text
Прочитай docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
и полностью выполни содержащуюся в нём задачу.
```

Исполнители должны писать пользователю только по-русски, докладывать прогресс на 50% и 90%, а подробные evidence сохранять в audit artifacts вместо публикации больших raw-отчётов в чате.
