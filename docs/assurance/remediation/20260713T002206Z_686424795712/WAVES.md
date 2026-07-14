# Remediation waves — 20260713T002206Z_686424795712

Этот overlay задаёт порядок исправления 17 подтверждённых findings, не меняя
исходные `.audit/**` artifacts и `functional/spark/QUEUE.csv`. Он не является
разрешением запустить Spark: выполнение допускается только новым fail-closed
протоколом `docs/audit/10_JARVIS_FUNCTIONAL_REMEDIATION_WAVES_PROMPT.md` от
явно указанного reviewed foundation commit.

## Зафиксированный контекст

| Назначение | SHA |
|---|---|
| Orchestration/foundation base | `b2c481de1a9e68079a67ff49790eb685a09e80e5` |
| Завершение PHASE B | `5aae9855f0779c746ec9287c2ec8917637fedb36` |
| Production HEAD, проверенный кампанией | `3fda655e4f723a0d8f58a4edfb4b3ee7dda079fe` |

Эти SHA не взаимозаменяемы. Ремедиация должна стартовать от отдельного
reviewed foundation commit — потомка orchestration base, содержащего permanent
QA harness и этот overlay. Проверенный production HEAD остаётся provenance
исходных результатов, а не точкой создания foundation worktree.

Исходное состояние: `COMPLETE_WITH_BLOCKERS`, progress 100%, 86 terminal
scenarios, 30 scenario FAIL, 49 operator-repeat FAIL, 17 findings и 17 READY
Spark tasks. `functional/spark/READY` существует; `functional/READY` намеренно
отсутствует и для remediation не требуется.

## Неизменяемые source pins

Четыре записи `immutable_sources` используют единственную конвенцию
`sha256_git_blob_raw_bytes_v1`: SHA-256 вычисляется по точным bytes Git blob из
точного `source_commit`, полученного через local Git object database. Checkout
bytes не являются источником, поэтому EOL-конверсия, text mode и нормализация
не участвуют. Каждая запись фиксирует commit, repository-relative path, Git
blob OID и SHA-256 raw blob bytes.

До wave verifier обязан получить `4/4` source pins и `17/17` однозначных
task/finding/path mappings из того же commit. Он сравнивает overlay с exact
QUEUE/index/task blobs, не читает mutable checkout sources и не обращается к
сети. Весь overlay разбирается только как canonical dependency-free YAML
subset; mappings берутся из фактических `waves` и `product_decision_gate`.
Block scalar, shadow/duplicate section или alternate noncanonical structure
отклоняются до проверки mappings:

```powershell
py -3.11 -m qa.cli validate-overlay-sources `
  --repository-root <repository-root> `
  --overlay docs/assurance/remediation/20260713T002206Z_686424795712/WAVES.yml `
  --expected-source-commit 5aae9855f0779c746ec9287c2ec8917637fedb36 `
  --git-executable <trusted-absolute-git-executable>
```

Любой missing/stale commit, path, blob OID, digest или task mapping — blocker.

## Неизменяемые правила

- Один запуск получает ровно один explicit `TARGET_WAVE`.
- Внутри wave задачи выполняются строго по одной и в указанном порядке.
- Одна terminal task соответствует ровно одному локальному commit; успешная
  task включает минимальный patch, regression test и task report.
- Исходный FAIL воспроизводится до patch. Невоспроизводимый FAIL не
  исправляется предположительно и останавливает wave точным blocker.
- После каждого логического блока проверяются scope, `git diff` и
  неизменность `.audit/**`.
- После batch validation запуск останавливается для review. Следующая wave не
  начинается автоматически.
- Между waves обязателен committed human review record с точным
  `reviewed_head` предыдущей wave.
- Запрещены push/merge, создание `functional/READY` и изменение исходной
  queue/task/evidence.

## Wave 0 — безопасность, правдивость и изоляция

Эта wave выполняется первой: до новых больших прогонов нельзя доверять
секретам в diagnostics, exit status, содержимому ответа и изоляции UI/runtime.

| Порядок | Task | Finding | Gate |
|---:|---|---|---|
| 1 | `SPARK-0017` | `FUNC-FIND-017` | Token redaction во всех doctor outputs |
| 2 | `SPARK-0016` | `FUNC-FIND-016` | Truthful doctor exit code и clean test env |
| 3 | `SPARK-0006` | `FUNC-FIND-006` | Нет internal tool envelopes в stream/final/DOM |
| 4 | `SPARK-0009` | `FUNC-FIND-009` | Canonical approved action и exact state change |
| 5 | `SPARK-0015` | `FUNC-FIND-015` | Transcript изолирован по runtime identity |
| 6 | `SPARK-0011` | `FUNC-FIND-011` | Interrupted stream не оставляет stale placeholder |

Выход: шесть task commits, task-specific tests и bounded replay PASS, batch
validation зафиксирована в последнем task commit, состояние
`WAVE_0_CANDIDATE_FOR_REVIEW`. Любой обязательный FAIL/blocker не позволяет
объявить candidate.

## Wave 1 — lifecycle и документы

Precondition: committed review `WAVE-0` со статусом `APPROVED` и точным
`reviewed_head`.

| Порядок | Task | Finding | Gate |
|---:|---|---|---|
| 1 | `SPARK-0014` | `FUNC-FIND-014` | Repeated start идемпотентен |
| 2 | `SPARK-0007` | `FUNC-FIND-007` | Stable uploaded-document identity/recall |
| 3 | `SPARK-0003` | `FUNC-FIND-003` | Atomic path/write/verification и source preservation |
| 4 | `SPARK-0008` | `FUNC-FIND-008` | Corrupt-to-valid recovery без stale state |

Выход: четыре task commits, bounded validation, остановка в
`WAVE_1_CANDIDATE_FOR_REVIEW`.

## Wave 2 — operator behavior

Precondition: committed review `WAVE-1` со статусом `APPROVED` и точным
`reviewed_head`.

| Порядок | Task | Finding | Gate |
|---:|---|---|---|
| 1 | `SPARK-0002` | `FUNC-FIND-002` | Exact count/JSON/assumption constraints |
| 2 | `SPARK-0004` | `FUNC-FIND-004` | Conversation-local multi-turn references |
| 3 | `SPARK-0005` | `FUNC-FIND-005` | Clarification before mission/artifact |
| 4 | `SPARK-0012` | `FUNC-FIND-012` | Exact memory namespace isolation |
| 5 | `SPARK-0001` | `FUNC-FIND-001` | DNS/network routing без shopping |
| 6 | `SPARK-0010` | `FUNC-FIND-010` | Supported cited synthesis или точный blocker |

Выход: шесть task commits, bounded validation, остановка в
`WAVE_2_CANDIDATE_FOR_REVIEW`. Это не создаёт product READY: отдельная
post-fix acceptance campaign остаётся обязательной.

## Product decision gate — SPARK-0013 / FUNC-FIND-013

`SPARK-0013` исключён из numbered waves. Его исходный acceptance текст нельзя
трактовать как обещание «сделать 31B быстрым». Решение разделено на:

- `PROFILE-SAFETY` — fail-closed metadata/readiness/health policy и сокрытие
  non-certified profiles из normal interactive selection;
- `PROFILE-RESEARCH` — отдельный bounded spike без гарантии production fix и
  без скачивания engine/model без нового approval.

До отдельного human decision единственный подтверждённый interactive profile
на certified host — `gemma4-turbo`. Полные gates описаны в
`PROFILE_DECISION.md`.
