# Product decision gate — 31B profiles

Status: `PENDING_HUMAN_DECISION`

Source task: `SPARK-0013` / `FUNC-FIND-013`

Profiles: `gemma4-mono-perf`, `gemma4-mono`

`SPARK-0013` не входит в `WAVE-0`, `WAVE-1` или `WAVE-2` и не может быть
запущен автоматически. Исходная формулировка acceptance не является
обещанием «сделать 31B быстрым». Требуются отдельное product decision,
reviewed scope и отдельный execution protocol.

## Подтверждённое исходное состояние

- `gemma4-turbo` — единственный подтверждённый interactive profile на
  certified host на момент кампании.
- `gemma4-mono-perf` стал ready примерно через 3.5 минуты, но три direct
  probes возвращали повторяющийся `cyclic`; GUI деградировал или завершался
  fallback.
- `gemma4-mono` пересёк 20-минутный readiness deadline; один direct completion
  занял 47.25 секунды и также вернул `cyclic`, при наблюдаемом decode около
  0.1–0.4 tok/s.
- Identity mapping была правдивой; проблема относится к functional
  quality/readiness, а не к подмене имени модели.

Эти факты не доказывают, что конкретная конфигурация исправима на данном host,
и не разрешают скачивать другой engine/model.

## PROFILE-SAFETY

Цель — сделать выбор профиля и readiness fail closed независимо от успеха
исследований производительности.

Обязательный контракт:

1. Каждый профиль имеет явный status:
   `certified`, `experimental` или `unsupported`.
2. Каждый start имеет числовой readiness deadline и машинно-читаемую причину
   отказа после deadline; fake-clock tests проверяют границу.
3. До объявления interactive readiness выполняется direct output health probe.
4. Repeated/cyclic/empty/degenerate output приводит к fail-closed отказу, а не
   к ready, fallback или ложному success.
5. `experimental` и `unsupported` profiles скрыты из normal interactive
   selection. Их запуск возможен только через явно обозначенный research path.
6. CLI/API/GUI правдиво показывают profile status, loaded model identity,
   deadline, probe result и blocker.
7. Отказ профиля не меняет production state и не вызывает неявный переход на
   другой model/profile.

Минимальная acceptance для самого safety contract:

- deterministic metadata/schema tests PASS;
- fake-clock deadline tests PASS для обоих 31B profiles;
- repeated-token, empty-output и timeout fixtures отклоняются;
- normal selector показывает только `certified` profiles;
- отсутствие real model или успешной live probe не повышает profile до
  `certified`.

Выполнение `PROFILE-SAFETY` само по себе не закрывает `SPARK-0013` как
production-quality fix. Оно может завершиться правдивым состоянием
`experimental` или `unsupported`.

## PROFILE-RESEARCH

Цель — bounded spike по model/chat-template/runtime configuration без
гарантированного production fix.

До запуска human decision обязан зафиксировать:

- точный host и resource budget;
- max wall-clock duration и startup deadline;
- допустимые уже локально доступные model/engine artifacts;
- bounded configuration matrix;
- output/latency/GUI acceptance metrics;
- stop conditions и cleanup/rollback steps;
- запрет production promotion без отдельного review.

Правила:

- отдельный isolated worktree и runtime namespace;
- никаких external engine/model downloads без нового явного approval;
- никаких новых dependencies, repositories, containers или binaries без
  отдельного approval;
- никаких production state/config changes;
- не исполнять внешний код;
- сохранять только sanitized evidence и canary credentials;
- отрицательный или inconclusive результат является допустимым результатом
  spike и не превращается в speculative production patch.

## Promotion gate для каждого 31B profile

Profile может перейти в `certified` только после отдельного review, если
одновременно:

1. задекларирован числовой readiness deadline и он соблюдён на certified host;
2. три live direct probes при temperature 0 отвечают `4` на `2+2` и не
   содержат repeated/cyclic/degenerate output;
3. один bounded GUI direct-answer case завершается без fallback раньше
   configured request timeout;
4. identity, finish reason, latency и output health сохранены в sanitized
   evidence;
5. task-specific deterministic tests, failure fixtures и rollback checks PASS;
6. profile проходит отдельно утверждённый representative acceptance subset.

До выполнения всех шести условий profile остаётся `experimental` или
`unsupported` и скрыт из normal interactive selection.

## Требуемое решение пользователя

Перед любой работой по `SPARK-0013` пользователь должен отдельно выбрать и
одобрить один или оба scoped tracks:

```text
PROFILE-SAFETY: APPROVE | REJECT | DEFER
PROFILE-RESEARCH: APPROVE | REJECT | DEFER
```

Для каждого `APPROVE` нужны reviewed execution protocol, allowed files,
resource/time budget, exact base commit и rollback manifest. Этот foundation
документ не делает выбор от имени пользователя.
