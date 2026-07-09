# JARVIS GPT Architecture

## Принцип

JARVIS GPT — локальный agent runtime. UI не управляет моделью напрямую: он работает с backend-ядром, а ядро уже решает, нужен ли LLM, память, mission plan, инструмент или диагностика.

## Слои

```text
Command Center (Next.js)
  |
FastAPI Gateway
  |
Agent Runtime
  |-- operator persona (who I am)
  |-- conversation context
  |-- mission planner
  |-- safe tools registry
  |-- memory lookup
  |-- file ingestion and chunk search
  |-- audit trail
  |-- HITL approval gates
  |-- telemetry and learning tick
  |-- autonomous supervisor
  |-- background cognition pulse
  |-- long-lived autonomy executor
  |-- host bridge status and gated execution
  |-- task lifecycle
  |-- diagnostics
  |-- event stream
  |-- model catalog
  |-- operator queue
  |-- memory hygiene
  |-- model profile roadmap
  |-- web evidence synthesis
  |-- isolated headless web render
  |-- per-answer observable trace
  |-- answer quality dashboard
  |-- capability/current-work manifest
  |-- result integrity (self-check, repair, mission deliverable, clarify)
  |-- experience loop (operator feedback, outcome lessons, lessons-in-prompt)
  |
LLM Router
  |
OpenAI-compatible Gemma dispatcher
  |
Optional Docker Compose profile `llm`

External host runtime
  D:\jarvis\models
  D:\jarvis\data\models
  D:\jarvis\data
  D:\jarvis\cache
  D:\jarvis\logs
  D:\jarvis\docker
```

## Почему так

- Репозиторий не хранит тяжёлые артефакты.
- Backend можно запускать нативно, в WSL2 или Docker.
- SQLite достаточно для одиночного локального ядра и легко мигрирует дальше.
- UI видит явные состояния: success, warn, error, mission, event.
- LLM является заменяемым маршрутом, а не фундаментом всей системы.
- Operator persona — первоклассный слой понимания оператора: durable профиль (роль, домашний город, языки, стек, увлечения, текущий фокус, постоянные правила, глоссарий) читается на каждом ходу и обобщает узкие маршруты (например, домашний город закрывает погоду/локальные/гео запросы вместо отдельного weather-кэша). Это широкое поле правок вместо патча под каждый юзкейс.
- Reasoning-first интент: для fuzzy web-семьи И для локального bucket (запросы о состоянии/действиях на машине оператора) агент не доверяет каскаду `_looks_like_*`, а спрашивает модель (`_understand_intent`), которая понимает задачу по смыслу и operator-контексту и решает маршрут. Арбитр может вернуть `web_research | reasoning | local_action | mission | chat | clarify`, и каждое решение обрабатывается: `local_action` уводит запрос в агентный loop с нативными инструментами (system.inspect для чтения состояния, windows.native под approval для мутаций) вместо интернет-поиска локального состояния. Эвристики остаются детерминированным фолбэком (офлайн и для конкретных matched tool-биндингов вроде явного native OS action, который срабатывает ДО арбитра). Так «понимание задачи» вытесняет «правила-затычки» и на web-, и на локальном маршруте, не ломая деградацию без LLM.
- Web evidence synthesis: прямой web-маршрут теперь не заканчивается механическим списком ссылок. Backend собирает search/fetch/render evidence, присваивает источникам простой quality label, затем LLM делает краткий вывод только из этих фактов. Если synthesis даёт мусор или router JSON, включается старый deterministic formatter.
- JS-heavy web pages go through backend-owned `web.render`: isolated headless Chrome/Edge, temporary profile, public-only DNS pinning, no operator browser tabs. The normal route tries `web.fetch` first and falls back to render when fetched text is too thin or unavailable.
- Web SSRF protection is enforced both before request setup and at connect time: `web.search`/`web.fetch` use a public-only httpx transport that resolves, validates, and pins public IPs before opening TCP, closing the old DNS-rebinding window without giving the model local network reach.
- Per-answer trace: сохранённый assistant message можно открыть как `/trace/{messageId}`. Backend отдаёт предыдущий user input, assistant output, события runtime и nodes/edges граф; UI показывает анимированный путь сигнала без раскрытия hidden chain-of-thought.
- Агентный tool-loop: на пути ответа модель — не одиночный forward-pass, а цикл, где она сама выбирает безопасные инструменты, читает observation и продолжает до финального ответа. Опасные инструменты автономно не выполняются, а становятся approval-гейтами. Это снимает «чат-бот»-стену (у модели появляются руки), не завися от размера модели.
- Гибридный retrieval: память достаётся не только лексически (BM25/LIKE), но и семантически (fuzzy-вектор или remote-эмбеддинги), фьюз через RRF над ограниченным пулом кандидатов. Модель получает релевантный контекст даже при перефразировании — это отдельная подсистема, которую нельзя «дообучить» размером чат-модели.
- Реальное исполнение миссий: `execute_next_mission_step` при живом LLM прогоняет шаг через агентный tool-loop (реальные вызовы инструментов, approval для опасного, аудит), а не отдаёт статичный brief. Миссии перестали быть «планами, которые ничего не делают». Офлайн остаётся детерминированный brief.
- Авто-цепочка миссий: `run_mission` последовательно исполняет шаги до завершения/блокировки/бюджета, не обходя approval-гейты. Command Center гоняет цепочку клиентски (по `execute-next`), давая живой прогресс без WS; серверный `/run` — для headless.
- Опасные действия не входят в safe tools layer; shell/host control идёт через token-auth host bridge и HITL-gates.
- Read-only инспекция машины оператора отделена от мутирующего нативного слоя: `system.inspect` (safe) даёт агентному tool-loop WMI/CIM SELECT и список окон, а `windows.native` (danger, approval) держит process.start/keyboard.send/фокус. Это разблокирует понимание модели: на бытовой запрос о состоянии машины (железо, ОС, диски, оперативка, батарея, службы, автозагрузка, принтеры, сеть) модель сама выбирает Win32_* класс по своему знанию и читает состояние — вместо keyword-таблицы из 5 классов, которая срабатывала только на слове «wmi». Тот же сдвиг «понимание вместо затычек», что арбитр сделал для web_research, но через safe-инструмент, а не через переписывание маршрута.
- Файлы попадают в runtime-хранилище через upload/CLI, а агент читает только индексированные чанки через safe tools.
- Audit log фиксирует изменения памяти, миссий, task lifecycle, tool runs и ingestion.
- Профили `gemma4-mono` и `gemma4-turbo` указывают на реальные каталоги весов в `D:\jarvis\data\models`; backend не хранит веса в репозитории.
- Dispatcher вынесен в отдельный Compose profile `llm`, чтобы Command Center можно было запускать без случайной загрузки тяжёлых весов в VRAM.
- Любое действие с риском выше safe должно сначала стать approval gate; выполнение после approve проходит через отдельный whitelisted gated executor.
- Self-learning идёт через append-only learning journal и `learning.tick`: диалоги, tool runs, web/browser observations и deletion markers превращаются в lessons без привязки к видимой истории чатов.
- When the local LLM is enabled, learning tick also runs a bounded JSON-only distillation pass over recent signals, adding at most two grounded lessons on top of deterministic lessons. The quality dashboard exposes recent negative feedback, verifier revise signals, and repeated gaps for operator/assistant review.
- Background cognition is an observational supervisor loop, not another UI
  request: when enabled it periodically asks the local LLM to summarize recent
  runtime/learning/autonomy signals into strict JSON, persists
  `cognition.last_pulse`, and writes a `cognition.pulse` learning observation.
  It does not browse, mutate the host, or create jobs automatically.
- Устойчивость слоя целостности: самопроверка и ремонт запускаются после готового черновика, поэтому у них отдельный таймаут-бюджет (`VERIFY_TIMEOUT_SEC`, не больше `llm_timeout_sec`) — зависший критик деградирует до отдачи черновика, а не держит готовый ответ на полный LLM-таймаут. Это тот же принцип «сбой контроля качества не портит хороший результат», но на оси латентности.
- Контракт API проверяется end-to-end: `test_api_smoke.py` поднимает реальное ASGI-приложение (offline, autonomy off) и проходит критичный путь оператора, ловя регрессии роутинга (response_model, await, статус-коды), которые unit-тесты компонентов пропускают.
- Autonomous supervisor безопасно выполняет только наблюдение: telemetry snapshots и learning tick сразу при старте и далее по расписанию; действия с риском остаются через approvals.
- Persisted autonomy jobs now carry priority, deadline, runtime budget, cancel state, and run history. Due jobs are priority-aware, expired jobs cancel before execution, long jobs are timed out by their budget, and failed/cancelled jobs surface in the operator queue.
- Performance слой разделяет лёгкий backend и тяжёлый dispatcher; GPU утилизируется vLLM-профилем, а backend собирает telemetry без удержания весов.

- Persona учится сама через понимание, а не через regex: у модели есть safe-
  инструменты `persona.get`/`persona.insight`, и когда оператор мимоходом
  раскрывает устойчивый факт о себе, модель сохраняет его одним вызовом.
  `persona.insight` — единственная сознательно разрешённая в автономном цикле
  мутация: один факт за вызов, дедуп, пер-полевые капы, аудит и событие, правка
  доступна из Command Center. Так закрывается разрыв «add_insight есть, но агент
  его не вызывает» без каскада эвристик.
- Файловый retrieval не слепнет на перефразировании без общих словоформ: при
  пустом лексическом пуле включается ограниченный fallback по недавним чанкам с
  порогом fuzzy-связности, помеченный `semantic-recent`. Деградация прежняя:
  нет связанных чанков — нет файлового контекста, чужие файлы в промпт не
  попадают.
- Mission-детекция — тоже понимание, а не счётчик ключевых слов: если
  reasoning-first арбитр уверенно (>= 0.7) видит в задаче реальную многошаговую
  миссию, task kernel переписывается на mission-маршрут и создаётся обычный
  persisted mission plan. Ключевые слова остаются офлайн-фолбэком.
- Result integrity — backend-owned «definition of done»: substantive-ответ не
  уходит оператору непроверенным. Один бюджетный критик-проход сверяет черновик
  с задачей и completion criteria из task kernel, затем максимум один
  ремонт-раунд (rewrite для request/response, короткая поправка для стрима,
  переписанный отчёт для шага миссии). Сломанный критик или ремонт никогда не
  портит хороший ответ: любой сбой означает «ответ стоит как есть». Это
  архитектурный ответ на «модель уверенно ответила мимо задачи» — его не лечит
  размер модели, потому что первый forward-pass себя не проверяет.
- Миссия заканчивается результатом, а не прогресс-баром: при переходе в `done`
  синтезируется итоговый операторский отчёт (LLM с детерминированным fallback),
  сохраняется в память и KV, эмитится событием и отдаётся через
  `final_report` / `GET /api/missions/{id}/report`. Отчёт идемпотентен.
- Понимание включает право спросить: арбитр может вернуть `clarify` с одним
  точным вопросом, и Jarvis задаёт его вместо уверенной догадки. Порог выше
  обычного, промпт запрещает clarify при очевидном допущении — поэтому это
  инструмент против «уверенно не то», а не источник лишних переспросов.
- Петля опыта замкнута: сигналы исхода (оценки оператора 👍/👎 с комментарием,
  revise-вердикты самопроверки, отклонённые approval-гейты) попадают в
  append-only learning journal, learning tick превращает их в уроки с цитатами
  реального контекста, а топ-уроки детерминированно вставляются в промпт
  каждого хода и каждого шага миссии. Обучение перестало зависеть от того,
  «вспомнит» ли retrieval нужный урок; негативный опыт меняет поведение уже на
  следующем ходу и переживает удаление видимой истории чата.
- Mission approvals resume in-place: a gated mission tool stores a compact
  agentic snapshot, and the approval executor feeds the approved tool observation
  back into that same step before marking the task done/blocked.
- Operator queue is the shared runtime inbox: approvals, blocked missions,
  health warnings, generation truncation, memory hygiene, and future model-profile
  work are surfaced through `GET /api/operator/queue` and the Command Center queue
  tab instead of being scattered across separate panels.
- Generation resilience is backend-owned: when the LLM reports
  `finish_reason=length`, chat and stream paths request continuation internally
  and only show the old token-limit warning if the continuation budget is also
  exhausted.
- Model-profile planning is a roadmap layer for now. `GET /api/model-profiles`
  describes future planner/reviewer and fast-executor roles, but live routing
  remains on the current Gemma profile until stronger hardware and endpoints exist.

## Long-Lived Autonomy

- Persisted autonomy jobs can now run without a visible UI request. The supervisor
  periodically asks the shared `AutonomyExecutor` for due work.
- Mission jobs continue `run_mission` within budgets, preserve approval gates,
  pause when blocked, and finish when the mission is done.
- Normal chat and mission execution prompts include a compact
  capability/current-work manifest so the LLM sees safe tools, gated tools,
  current mission/conversation context, and existing background jobs.

## Runtime profiles

`gemma4-mono`:

- надёжный cold-start;
- eager mode;
- профиль разработки и диагностики.

`gemma4-turbo`:

- быстрый warmed runtime;
- больше шагов агента;
- включается после проверки окружения.
