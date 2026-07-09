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
  |-- per-answer observable trace
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
- Reasoning-first интент: для fuzzy web-семьи агент не доверяет каскаду `_looks_like_*`, а спрашивает модель (`_understand_intent`), которая понимает задачу по смыслу и operator-контексту и решает маршрут. Эвристики остаются детерминированным фолбэком (офлайн и для конкретных tool-биндингов вроде native OS action). Так «понимание задачи» вытесняет «правила-затычки», не ломая деградацию без LLM.
- Web evidence synthesis: прямой web-маршрут теперь не заканчивается механическим списком ссылок. Backend собирает search/fetch evidence, присваивает источникам простой quality label, затем LLM делает краткий вывод только из этих фактов. Если synthesis даёт мусор или router JSON, включается старый deterministic formatter.
- Per-answer trace: сохранённый assistant message можно открыть как `/trace/{messageId}`. Backend отдаёт предыдущий user input, assistant output, события runtime и nodes/edges граф; UI показывает анимированный путь сигнала без раскрытия hidden chain-of-thought.
- Агентный tool-loop: на пути ответа модель — не одиночный forward-pass, а цикл, где она сама выбирает безопасные инструменты, читает observation и продолжает до финального ответа. Опасные инструменты автономно не выполняются, а становятся approval-гейтами. Это снимает «чат-бот»-стену (у модели появляются руки), не завися от размера модели.
- Гибридный retrieval: память достаётся не только лексически (BM25/LIKE), но и семантически (fuzzy-вектор или remote-эмбеддинги), фьюз через RRF над ограниченным пулом кандидатов. Модель получает релевантный контекст даже при перефразировании — это отдельная подсистема, которую нельзя «дообучить» размером чат-модели.
- Реальное исполнение миссий: `execute_next_mission_step` при живом LLM прогоняет шаг через агентный tool-loop (реальные вызовы инструментов, approval для опасного, аудит), а не отдаёт статичный brief. Миссии перестали быть «планами, которые ничего не делают». Офлайн остаётся детерминированный brief.
- Авто-цепочка миссий: `run_mission` последовательно исполняет шаги до завершения/блокировки/бюджета, не обходя approval-гейты. Command Center гоняет цепочку клиентски (по `execute-next`), давая живой прогресс без WS; серверный `/run` — для headless.
- Опасные действия не входят в safe tools layer; shell/host control идёт через token-auth host bridge и HITL-gates.
- Файлы попадают в runtime-хранилище через upload/CLI, а агент читает только индексированные чанки через safe tools.
- Audit log фиксирует изменения памяти, миссий, task lifecycle, tool runs и ingestion.
- Профили `gemma4-mono` и `gemma4-turbo` указывают на реальные каталоги весов в `D:\jarvis\data\models`; backend не хранит веса в репозитории.
- Dispatcher вынесен в отдельный Compose profile `llm`, чтобы Command Center можно было запускать без случайной загрузки тяжёлых весов в VRAM.
- Любое действие с риском выше safe должно сначала стать approval gate; выполнение после approve проходит через отдельный whitelisted gated executor.
- Self-learning идёт через append-only learning journal и `learning.tick`: диалоги, tool runs, web/browser observations и deletion markers превращаются в lessons без привязки к видимой истории чатов.
- Autonomous supervisor безопасно выполняет только наблюдение: telemetry snapshots и learning tick сразу при старте и далее по расписанию; действия с риском остаются через approvals.
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
