# JARVIS — ASSURANCE & UPSTREAM FOUNDATION BOOTSTRAP

Ты выполняешь отдельный подготовительный этап между завершённой функциональной PHASE B и функциональными исправлениями Spark.

Этот этап **не исправляет production-поведение JARVIS**. Его задача — создать постоянный QA/assurance-контур, формализовать безопасное заимствование upstream-решений, устранить deadlock протокола запуска ремедиации и разложить подтверждённые findings по безопасным волнам.

Все сообщения пользователю — только на русском. Подробности сохраняй в репозитории; в чат выводи только прогресс, paths, commits, test counts и реальные blockers.

---

## 1. Зафиксированный контекст

Функциональная кампания:

```text
RUN_ID=20260713T002206Z_686424795712
functional path=.audit/runs/20260713T002206Z_686424795712/functional
tested production HEAD=3fda655e4f723a0d8f58a4edfb4b3ee7dda079fe
audit completion commit=5aae9855f0779c746ec9287c2ec8917637fedb36
```

Ожидаемое состояние:

```text
FUNCTIONAL_STATE.status=COMPLETE_WITH_BLOCKERS
FUNCTIONAL_STATE.markers.functional_ready=false
FUNCTIONAL_STATE.markers.spark_ready=true
functional/READY отсутствует
functional/spark/READY присутствует
17 findings
17 Spark tasks
```

Отсутствие `functional/READY` — ожидаемый результат: продукт не признан готовым. Это **не должно блокировать подготовку и выполнение ремедиации**.

Нынешний `docs/audit/07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md` содержит deadlock: он требует одновременно `functional/READY` и `functional/spark/READY`. Не запускай его и не создавай фиктивный `functional/READY`.

---

## 2. Каталоги и обязательная изоляция

Основной checkout:

```text
D:\jarvis-gpt
```

Новый worktree:

```text
D:\jarvis-gpt-worktrees\assurance-20260713T002206Z_686424795712
```

Новая ветка:

```text
foundation/assurance-upstream/20260713T002206Z_686424795712
```

Backup root:

```text
D:\jarvis\audit-backups\20260713T002206Z_686424795712\assurance-bootstrap
```

Основной checkout не изменяй. Не выполняй push, merge, rebase, squash, force-update, `git reset --hard`, aggressive clean или auto-stash.

Начни:

```powershell
Set-Location D:\jarvis-gpt
git rev-parse --show-toplevel
git status --short --branch
git rev-parse HEAD
git show --no-patch --oneline 5aae9855f0779c746ec9287c2ec8917637fedb36
```

Если основной checkout dirty вне ожидаемых локальных непубликуемых evidence, не присваивай и не удаляй изменения. Остановись с точным blocker.

### 2.1. Допустимый pre-existing untracked baseline

Наличие заранее зафиксированных untracked-файлов в основном checkout само по себе не блокирует foundation. Продолжать разрешено только при одновременно чистых tracked diff (`git diff --exit-code`) и index (`git diff --cached --exit-code`).

До создания worktree сохрани exact список untracked-путей вне repository. После завершения работы повторно сохрани список и сравни его с исходным baseline. Для этого run известный baseline включает локальные `.audit/**` artifacts и `JARVIS_FULL_AUDIT_PROMPT_FIXED.md`.

Файлы известного baseline запрещено stage, read, modify, move или delete. Появление нового tracked diff, нового staged path либо любое изменение состава untracked baseline является blocker.

Создавай worktree от точного commit SHA. Содержимое основного рабочего каталога, включая untracked baseline, не является источником для worktree.

Перед работой:

1. Создай annotated tag:

```text
pre-assurance-bootstrap-20260713T002206Z_686424795712
```

2. Создай Git bundle в backup root.
3. Выполни `git bundle verify`.
4. Сохрани SHA-256 bundle и manifest.
5. Создай ветку и worktree от текущего audit HEAD.
6. После перехода в worktree повторно проверь root/branch/HEAD/status.

---

## 3. Разрешённые входные данные

Прочитай:

```text
.audit/LATEST_FUNCTIONAL_RUN.txt
.audit/runs/20260713T002206Z_686424795712/functional/FUNCTIONAL_STATE.json
.audit/runs/20260713T002206Z_686424795712/functional/FUNCTIONAL_ASSURANCE_STATEMENT.md
.audit/runs/20260713T002206Z_686424795712/functional/FUNCTIONAL_FINDINGS_INDEX.md
.audit/runs/20260713T002206Z_686424795712/functional/RESIDUAL_GAPS.md
.audit/runs/20260713T002206Z_686424795712/functional/RESPONSE_INTEGRITY_REPORT.md
.audit/runs/20260713T002206Z_686424795712/functional/INSTRUCTION_FOLLOWING_REPORT.md
.audit/runs/20260713T002206Z_686424795712/functional/PROFILE_AND_MODEL_REPORT.md
.audit/runs/20260713T002206Z_686424795712/functional/GUI_AND_STREAMING_REPORT.md
.audit/runs/20260713T002206Z_686424795712/functional/DOCUMENT_AND_TOOL_REPORT.md
.audit/runs/20260713T002206Z_686424795712/functional/MISSION_MEMORY_REPORT.md
.audit/runs/20260713T002206Z_686424795712/functional/STARTUP_AND_RECOVERY_REPORT.md
.audit/runs/20260713T002206Z_686424795712/functional/spark/QUEUE.csv
.audit/runs/20260713T002206Z_686424795712/functional/spark/TASK_SCHEMA.md
.audit/runs/20260713T002206Z_686424795712/functional/spark/tasks/*.md
.audit/runs/20260713T002206Z_686424795712/functional/harness/README.md
.audit/runs/20260713T002206Z_686424795712/functional/harness/*.py
docs/audit/07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md
docs/audit/08_JARVIS_FUNCTIONAL_REMEDIATION_ROLLBACK_PROTOCOL.md
docs/assistant-notes.md
repository instructions
```

Не открывай и не копируй локальные несanitized evidence с фактическими credentials. Для тестов используй только disposable canary tokens. Не выполняй broad secret dump, env dump или `docker compose config` без redaction.

Не изменяй существующие `.audit/**` artifacts: они являются неизменяемым evidence завершённой кампании.

---

## 4. Жёсткая граница scope

На этом этапе запрещено изменять:

```text
backend/src/**
frontend/app/**
docker-compose.yml
scripts/doctor.ps1
scripts/jarvis-launcher.ps1
production configuration
runtime state under D:\jarvis
```

Разрешены только:

```text
qa/**
docs/assurance/**
docs/upstream/**
docs/audit/09_JARVIS_ASSURANCE_UPSTREAM_FOUNDATION_PROMPT.md
docs/audit/10_JARVIS_FUNCTIONAL_REMEDIATION_WAVES_PROMPT.md
tests located under qa/**
small repository metadata required only for the new QA developer tooling
```

Не устанавливай новые pip/npm/system dependencies. Не скачивай модели, containers, repositories или binaries. Не исполняй код из внешних репозиториев.

Если для foundation требуется production-source change, зафиксируй его как отдельную будущую task и не делай его сейчас.

---

## 5. Foundation A — постоянный QA/assurance harness

Подними общие части завершённого functional harness из `.audit/**/functional/harness` в постоянный developer-only контур.

Целевая структура может быть уточнена, но должна оставаться компактной:

```text
qa/
  README.md
  __init__.py
  cli.py
  models.py
  scenario_loader.py
  runner.py
  evidence.py
  redaction.py
  validators/
    __init__.py
    response_integrity.py
    stream_integrity.py
    artifacts.py
    state.py
    format_contracts.py
  review/
    __init__.py
    schemas.py
    reviewer.py
    adjudicator.py
    independence.py
  replay.py
  suites/
    operator_core/
    response_integrity/
    tools_and_approvals/
    documents/
    missions_and_memory/
    startup_and_recovery/
    injection/
  schemas/
    scenario.schema.json
    evidence.schema.json
    review.schema.json
    verdict.schema.json
  tests/
```

Не делай runtime import из `.audit/**`. Перенеси только обобщаемую логику и укажи provenance в комментариях/документации.

### 5.1. Обязательные свойства runner

Runner должен:

- принимать base URL только для loopback;
- использовать `trust_env=False` для loopback HTTP;
- выполнять CLI только через `shell=False` и явный allowlist;
- работать только с уникальным campaign ID и namespace;
- писать append-only JSONL evidence после каждого case;
- создавать файлы exclusive-create и не перезаписывать старые runs;
- иметь exit codes `0=PASS`, `1=FAIL`, `2=INCOMPLETE`, `3=HARNESS_ERROR`;
- различать `PASS`, `FAIL`, `INCONCLUSIVE`, `BLOCKED_BY_ENV`, `BLOCKED_BY_SPEC`, `SKIP`, `ERROR`;
- запрещать пустой PASS без фактических assertions;
- редактировать credential-like значения до записи;
- поддерживать offline replay сохранённого sanitized evidence;
- не запускать и не останавливать JARVIS самостоятельно в первой версии.

### 5.2. Детерминированные validators

Реализуй минимум:

1. exact language/format/count checks;
2. valid JSON parse/schema when JSON requested;
3. forbidden internal markers:
   - `call:`;
   - tool/function envelopes;
   - role/protocol markers;
   - traceback;
   - transport frames;
   - internal schemas;
4. duplicate/truncated/empty final detection;
5. NDJSON reconstruction and terminal-state consistency;
6. file existence, exact path, hash and source-unchanged checks;
7. conversation/runtime identity isolation checks;
8. claimed action versus observed state checks;
9. canary secret absence in stdout/stderr/JSON/logs;
10. process exit code versus machine-readable result consistency.

Детерминированный FAIL нельзя отменить LLM-судьёй.

### 5.3. Несколько независимых тестировщиков

Создай типизированные review packets и reviewer interface.

Каждый review должен иметь explicit independence level:

```text
DETERMINISTIC_ONLY
SAME_MODEL_CLEAN_CONTEXT
DIFFERENT_PROFILE
DIFFERENT_MODEL
DIFFERENT_PROVIDER
HUMAN_ADJUDICATED
```

Правила:

- reviewer получает только sanitized request, expected contract, actual output и bounded evidence;
- reviewer не видит verdict другого reviewer;
- два semantic review выполняются в чистых контекстах;
- disagreement без детерминированного решения даёт `INCONCLUSIVE`;
- reviewer не имеет права менять файлы, runtime или evidence;
- same-model clean-context reviews не называются независимыми моделями;
- adjudicator сохраняет обе исходные оценки и объясняет решение;
- отсутствие evidence никогда не превращается в PASS.

В foundation допустимы provider adapters и replay fixtures. Не требуется реальный внешний API key и запрещено добавлять secrets в repository.

### 5.4. CLI

Предоставь developer-only команды, например:

```powershell
py -3.11 -m qa.cli validate-suite qa\suites\operator_core
py -3.11 -m qa.cli replay <evidence.jsonl>
py -3.11 -m qa.cli validate-evidence <evidence.jsonl>
py -3.11 -m qa.cli build-review-packets <evidence.jsonl>
py -3.11 -m qa.cli adjudicate <review-1.json> <review-2.json>
```

Названия можно уточнить, но команды должны быть документированы и покрыты тестами.

---

## 6. Foundation B — upstream adoption gate

Создай:

```text
docs/upstream/
  ADOPTION_POLICY.md
  COMPONENT_ORIGINS.yml
  DONOR_REGISTRY.yml
  CANDIDATE_SCHEMA.json
  PROVENANCE_SCHEMA.json
  DECISION_TEMPLATE.md
  candidates/
  decisions/
```

И небольшой offline validator под `qa/upstream/` либо `qa/validators/`.

### 6.1. Типы происхождения

Поддержи:

```text
internal_human
commissioned_internal
inspired_by
external_dependency
external_adapter
vendored
ported_code
forked
generated_fixture
```

В `COMPONENT_ORIGINS.yml` обязательно зафиксируй:

```yaml
web_surfer:
  origin_kind: commissioned_internal
  commissioned_by: Dest
  implementation_agent: Claude
  external_code_imported: false

document_surfer:
  origin_kind: commissioned_internal
  commissioned_by: Dest
  implementation_agent: Grok
  external_code_imported: false
```

Не делай юридических утверждений об авторстве модели. Это engineering provenance.

### 6.2. Fail-closed adoption policy

Ни один внешний кандидат не может быть принят без:

- конкретного reproduced finding или capability gap;
- точного repository URL;
- pinned commit SHA;
- подтверждённой лицензии из самого repository;
- списка исходных файлов;
- выбранного adoption mode;
- dependency/security review;
- изолированного spike;
- regression/failure/rollback tests;
- provenance record;
- human approval.

Adoption modes:

```text
idea_only
test_corpus
external_dependency
black_box_adapter
ported_module
fork
```

По умолчанию механизм может исследовать, регистрировать и предлагать, но не может:

- копировать код;
- устанавливать зависимости;
- исполнять скачанный код;
- менять production source;
- автоматически merge/push;
- считать popularity доказательством пригодности.

Лицензии:

```text
MIT/BSD/Apache-like -> candidate only after notice verification
GPL/AGPL/LGPL/MPL -> mandatory explicit review
custom/source-available -> blocked pending review
no license -> code copying forbidden
unknown -> blocked
```

Это инженерный policy, не юридическое заключение.

### 6.3. Candidate validator

Offline validator должен проверять schema и обязательные поля, но не притворяться, что лицензия или безопасность подтверждены без evidence.

Добавь тесты:

- missing commit SHA -> FAIL;
- unknown license -> BLOCKED;
- no finding/capability gap -> FAIL;
- copied-code adoption without source files/provenance -> FAIL;
- idea-only adoption не требует копирования файлов;
- commissioned internal component не требует upstream repository.

Не добавляй внешние проекты как подтверждённых доноров. Допустимо создать `UNVERIFIED` placeholders только как future research backlog.

---

## 7. Foundation C — исправление orchestration deadlock

Не редактируй завершённый `FUNCTIONAL_STATE.json` и не создавай `functional/READY`.

Создай новый:

```text
docs/audit/10_JARVIS_FUNCTIONAL_REMEDIATION_WAVES_PROMPT.md
```

Он должен заменить `07` только для этого run и будущих аналогичных `COMPLETE_WITH_BLOCKERS` campaigns.

### 7.1. Корректные preconditions

Remediation разрешена, когда:

```text
FUNCTIONAL_STATE.status in {COMPLETE_WITH_BLOCKERS, COMPLETE}
FUNCTIONAL_STATE.progress_percent == 100
FUNCTIONAL_STATE.markers.spark_ready == true
functional/spark/READY существует
scenario queue не содержит NOT_RUN
каждый FAIL связан с finding/task
assurance foundation имеет reviewed local commit
```

`functional/READY`:

- не требуется для запуска исправлений;
- ожидаемо отсутствует при подтверждённых дефектах;
- может появиться только после успешной post-fix acceptance кампании.

Поле `operator_ready` из старого schema не используй как product-readiness signal. В новой документации называй это `operator_suite_complete` или `remediation_input_ready`.

### 7.2. Только одна remediation wave за запуск

Новый prompt должен:

- принимать/читать один explicit target wave;
- создавать отдельный Spark worktree по rollback protocol;
- выполнять задачи по одной;
- делать один local commit на задачу;
- останавливаться после batch validation текущей wave;
- не push/merge;
- не продолжать следующую wave автоматически;
- требовать review между waves;
- использовать permanent QA harness для replay и post-fix evidence.

---

## 8. Foundation D — overlay-план remediation waves

Не переписывай исходную `functional/spark/QUEUE.csv`. Создай overlay:

```text
docs/assurance/remediation/20260713T002206Z_686424795712/
  WAVES.md
  WAVES.yml
  PROFILE_DECISION.md
  ACCEPTANCE_MAP.md
```

Минимальное разбиение:

### Wave 0 — безопасность, правдивость и изоляция

Порядок:

```text
SPARK-0017  token redaction
SPARK-0016  doctor exit-code truthfulness
SPARK-0006  internal tool-envelope leak
SPARK-0009  canonical approval action
SPARK-0015  runtime-home transcript isolation
SPARK-0011  interrupted-stream placeholder cleanup
```

Причина: до следующих больших прогонов нельзя доверять evidence, success status, secrets и UI isolation.

### Wave 1 — lifecycle и документы

```text
SPARK-0014  repeated-start idempotency
SPARK-0007  stable uploaded-document identity/recall
SPARK-0003  atomic artifact path/write/verification
SPARK-0008  corrupt-document recovery
```

### Wave 2 — operator behavior

```text
SPARK-0002  exact response constraints
SPARK-0004  multi-turn references
SPARK-0005  clarification before mission
SPARK-0012  memory namespace
SPARK-0001  DNS/network versus shopping routing
SPARK-0010  cited usable web synthesis
```

### Product decision gate — 31B profiles

`SPARK-0013` нельзя автоматически трактовать как обещание «сделать 31B быстрым».

Раздели решение концептуально на:

```text
PROFILE-SAFETY:
  certified/experimental/unsupported metadata
  numeric readiness deadline
  direct output health probe
  fail-closed refusal on cyclic/degenerate output
  hide non-certified profiles from normal interactive selection

PROFILE-RESEARCH:
  bounded spike for model/chat-template/runtime configuration
  no guaranteed production fix
  no external engine/model download without separate approval
```

На текущем certified host только `gemma4-turbo` может считаться подтверждённым interactive profile, пока 31B не пройдут live health and bounded GUI acceptance.

Не меняй production profile code в foundation; только зафиксируй решение и точные будущие acceptance criteria.

---

## 9. Calibration без изменения production

Foundation должен доказать собственную работоспособность на сохранённом sanitized evidence.

Обязательно:

1. Replay нескольких PASS/FAIL/INCONCLUSIVE cases из завершённой кампании.
2. Validator обнаруживает известные:
   - tool-envelope leak;
   - empty/duplicate final;
   - secret canary;
   - exit-code/result mismatch;
   - artifact path/hash mismatch;
   - cross-runtime transcript mismatch.
3. Два synthetic semantic reviews дают раздельные immutable outputs.
4. Adjudicator:
   - сохраняет disagreement;
   - не отменяет deterministic FAIL;
   - возвращает `INCONCLUSIVE` при недостатке evidence.
5. Upstream schemas/policies проходят positive и negative fixtures.
6. Ни один тест не требует запущенного JARVIS, Docker, model или external network.

Не используй фактический runtime API token. Только canary values.

---

## 10. Tests и validation

Минимум:

```powershell
py -3.11 -m pytest qa\tests -q
py -3.11 -m qa.cli validate-suite qa\suites\operator_core
py -3.11 -m qa.cli validate-evidence <sanitized-fixture>
py -3.11 -m qa.cli replay <sanitized-fixture>
py -3.11 -m compileall qa
git diff --check
```

Если в repository уже используется Ruff:

```powershell
py -3.11 -m ruff check qa
```

Не запускай полный live model suite на этом этапе.

Проверь secret scan только по staged diff и generated test outputs с canary. Не читай локальный raw evidence с реальным token.

---

## 11. Commit policy

Предпочтительно три локальных commits:

```text
1. assurance: add permanent deterministic QA harness
2. assurance: add independent review and adjudication contracts
3. upstream: add provenance/adoption gate and remediation-wave protocol
```

Допустим другой небольшой и логичный split.

Каждый commit:

- только в foundation branch;
- включает tests;
- не содержит production behavior change;
- не содержит secrets;
- проходит `git diff --check`.

Не push и не merge.

---

## 12. Обязательные deliverables

Создай:

```text
docs/assurance/ARCHITECTURE.md
docs/assurance/INDEPENDENCE_LEVELS.md
docs/assurance/EVIDENCE_AND_VERDICTS.md
docs/assurance/BOOTSTRAP_SUMMARY_20260713T002206Z_686424795712.md

docs/upstream/ADOPTION_POLICY.md
docs/upstream/COMPONENT_ORIGINS.yml
docs/upstream/DONOR_REGISTRY.yml
docs/upstream/CANDIDATE_SCHEMA.json
docs/upstream/PROVENANCE_SCHEMA.json
docs/upstream/DECISION_TEMPLATE.md

docs/assurance/remediation/20260713T002206Z_686424795712/WAVES.md
docs/assurance/remediation/20260713T002206Z_686424795712/WAVES.yml
docs/assurance/remediation/20260713T002206Z_686424795712/PROFILE_DECISION.md
docs/assurance/remediation/20260713T002206Z_686424795712/ACCEPTANCE_MAP.md

docs/audit/10_JARVIS_FUNCTIONAL_REMEDIATION_WAVES_PROMPT.md
```

И machine-readable bootstrap state:

```text
docs/assurance/bootstrap/20260713T002206Z_686424795712/STATE.json
```

Финальное состояние:

```text
ASSURANCE_FOUNDATION_CANDIDATE_FOR_REVIEW
```

Не создавай product READY markers.

---

## 13. Acceptance gate

Foundation PASS только если:

- основной checkout неизменён;
- branch/worktree/base SHA зафиксированы;
- bundle проверен;
- existing `.audit/**` не изменены;
- production source/frontend/runtime config не изменены;
- protocol deadlock устранён новым remediation prompt;
- permanent QA harness не зависит runtime-импортом от `.audit`;
- deterministic validators ловят known failure fixtures;
- reviewer independence labels и adjudication работают;
- deterministic FAIL невозможно повысить до PASS;
- canary secrets отсутствуют в outputs;
- upstream provenance schemas fail closed;
- оба `_surfer` записаны как `commissioned_internal`;
- original Spark queue сохранена;
- remediation overlay разбит по waves;
- `SPARK-0013` вынесен в product decision/research gate;
- tests и compile/diff checks PASS;
- нет push/merge.

При невыполнении любого пункта не объявляй foundation готовым.

---

## 14. Финальный ответ

Кратко укажи:

- run ID;
- worktree/branch/base/final HEAD;
- commits;
- созданные paths;
- test counts;
- replay/calibration results;
- upstream policy validation;
- подтверждение отсутствия production changes;
- подтверждение неизменности `.audit`;
- остаточные blockers;
- готов ли `docs/audit/10_JARVIS_FUNCTIONAL_REMEDIATION_WAVES_PROMPT.md` к запуску Wave 0;
- финальное состояние `ASSURANCE_FOUNDATION_CANDIDATE_FOR_REVIEW`.
