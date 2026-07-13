# JARVIS — КАНОНИЧЕСКАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ АУДИТА И ИСПРАВЛЕНИЙ

Используй последовательность ниже. Она предназначена для проверки обычной работы JARVIS через Codex без загрузки старой расширенной очереди, которая несколько раз останавливала сессию интерфейсом.

## PHASE A — облачный аудит репозитория

Уже выполнена через:

```text
docs/audit/01_JARVIS_REPOSITORY_ONLY_AUDIT_PROMPT.md
```

Результат хранится в:

```text
.audit/LATEST_STATIC_RUN.txt
.audit/runs/<RUN_ID>/
```

PHASE A остаётся источником карты проекта и контрактов, но её полная live-очередь больше не является каноническим входом для локального Codex.

---

## Перед новой локальной сессией

```powershell
Set-Location D:\jarvis-gpt
git status --short --branch
git fetch origin
git pull --ff-only
git rev-parse --short HEAD
```

`git pull --ff-only` допустим только без конфликтующих tracked-изменений. Не используй reset, stash или clean для сохранённых `.audit/**` и пользовательских файлов.

Если предыдущая попытка оставила modified/untracked `.audit/**`, не нажимай «Отменить» и не удаляй их. Новый functional launcher сначала создаст внешний checkpoint и продолжит в отдельном namespace.

---

## PHASE B — функциональная проверка живого JARVIS

Канонический prompt:

```text
docs/audit/06_JARVIS_FUNCTIONAL_RUNTIME_ACCEPTANCE_PROMPT.md
```

Запускается через новую сессию Codex Sol Ultra на целевой машине.

Эта фаза проверяет:

- обычные пользовательские задачи;
- точное следование языку, формату, scope и запретам;
- полезный законченный результат вместо vague handoff;
- отсутствие ложного «готово»;
- отсутствие служебных структур приложения в обычном чате;
- multi-turn context;
- документы, tools, missions, memory и GUI;
- profiles/model mapping;
- startup, streaming, cancel/retry/recovery;
- offline-ready работу;
- performance и bounded long run.

Она не читает старую полную live-очередь и старые findings целиком. Новые результаты создаются в:

```text
.audit/runs/<RUN_ID>/functional/
```

Успешное завершение создаёт:

```text
.audit/LATEST_FUNCTIONAL_RUN.txt
.audit/runs/<RUN_ID>/functional/READY
.audit/runs/<RUN_ID>/functional/spark/READY
```

Без обоих functional markers Spark не запускается.

### Запуск PHASE B

```text
Прочитай docs/audit/06_JARVIS_FUNCTIONAL_RUNTIME_ACCEPTANCE_PROMPT.md
и полностью выполни содержащуюся в нём задачу.

Предыдущие попытки могли оставить partial .audit artifacts.
Сначала сохрани их предусмотренным prompt checkpoint, не отменяй и не удаляй,
затем выполни новую functional campaign в отдельном namespace.
```

---

## PHASE C — функциональные исправления через Spark

Канонический prompt:

```text
docs/audit/07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md
```

Он использует:

```text
docs/audit/08_JARVIS_FUNCTIONAL_REMEDIATION_ROLLBACK_PROTOCOL.md
```

Spark читает только завершённый functional run и его атомарную очередь. Он не загружает старую расширенную очередь.

Обязательная изоляция:

```text
worktree: D:\jarvis-gpt-worktrees\functional-<RUN_ID>
branch:   spark-functional/<RUN_ID>
```

Spark:

- не правит основной `D:\jarvis-gpt`;
- не работает в default branch;
- создаёт task checkpoints и локальные commits;
- не делает push или merge;
- повторяет реальные ordinary-user journeys после исправлений;
- заканчивает локальной веткой-кандидатом.

Финальный state:

```text
FUNCTIONAL_CANDIDATE_FOR_REVIEW
```

### Запуск PHASE C

```text
Прочитай docs/audit/07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md
и полностью выполни содержащуюся в нём задачу.
```

---

## Старые extended-файлы

Следующие документы сохранены как расширенная инженерная спецификация, но **не являются каноническим входом для Codex**:

```text
docs/audit/02_SAFE_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md
docs/audit/02_JARVIS_LIVE_MACHINE_AUDIT_PROMPT.md
docs/audit/03_SAFE_JARVIS_SPARK_REMEDIATION_PROMPT.md
docs/audit/03_JARVIS_SPARK_REMEDIATION_PROMPT.md
docs/audit/04_JARVIS_REMEDIATION_SAFETY_AND_ROLLBACK_PROTOCOL.md
```

Не запускай их повторно в Codex. Непроверенные части прежнего расширенного аудита остаются документированным `DEFERRED_REVIEW`, а не автоматически передаются Spark.

Все исполнители пишут пользователю только по-русски. Подробные результаты сохраняются в `.audit/**`; в чате достаточно процента, counts, paths и реальных blockers.