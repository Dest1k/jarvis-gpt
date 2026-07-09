# Runtime

## 2026-07-09 handoff - open browsing and durable learning journal

For the operator and the second model:

- Browser policy default is `open`: validated HTTP(S) URLs can be opened without
  approval. The validator still rejects non-http schemes and policy-locked URLs.
- `browser.open` and `browser.open_many` are intentionally denied from the
  autonomous tool loop. Background/current-data research should use backend
  `web.search` and `web.fetch`, which do not touch the operator's desktop browser.
- Added `learning_observations`: an append-only journal for dialogue messages,
  tool runs, web/browser observations, and conversation deletion markers. Deleting
  a chat removes visible history but leaves the learning journal intact.
- Learning tick now derives lessons from the journal as well as audit/tool/approval
  history. Supervisor runs learning immediately on startup and then every 120s by
  default.
- `GET /api/learning/journal` exposes recent learning observations for inspection.
- Command Center chat links are clickable for Markdown links, bare `http(s)` URLs,
  and `www.` URLs. Chat height now adapts to the viewport and the resize handle is
  no longer capped at 760px.

## 2026-07-09 handoff - operator queue and generation resilience

For the operator and the second model. This pass adds a thin runtime kernel
surface instead of another one-off UI rule:

- `GET /api/operator/queue` merges pending/executable approvals, blocked/running
  mission tasks, health warnings, lingering generation truncation, memory hygiene,
  and the future model-profile roadmap into one operator queue.
- `operator_context()` now exposes local runtime facts for prompts and UI:
  local time, active profile/model, operator name, home location, working roots,
  active missions, and pending approvals.
- Answers stopped by `finish_reason=length` are auto-continued internally for
  chat and stream paths. The old token-limit warning only appears if continuation
  still cannot finish the answer.
- Memory hygiene has explicit API surfaces: `GET /api/memory/hygiene` and
  `POST /api/memory/consolidate`. The report highlights duplicates, missing
  source tags, and low-confidence/stale notes.
- Model profiles are deliberately only scaffolded: `GET /api/model-profiles`
  reports current Gemma profiles plus future planner/reviewer and fast-executor
  roles, but no multi-model routing is active yet.
- Command Center opens on the new queue tab, shows mission/task links on approvals,
  and has one-click approve+execute for approved-gate recovery.
- Tests added: `test_agentic_answer_auto_continues_after_length_finish` and
  `backend/tests/test_operator_queue.py`.

## 2026-07-09 handoff - mission approval resume

For the operator and the second model. This closes the deeper approval follow-up
left by track 5.3: approving a gated mission tool no longer leaves the mission
to retry from scratch.

- Agent loop: `_run_agentic_tool` now writes `mission_id`, `task_id`, and a
  compact `resume` snapshot into approval payloads created from mission steps.
  Safe autonomous tool runs also receive mission/task ids, so the audit trail is
  attached to the mission.
- Approval executor: `tool.run` approvals execute the approved tool with
  `allow_danger=True` and mission/task ids, then call
  `AgentRuntime.resume_mission_after_approval` when the approval came from a
  mission step.
- Resume flow: the approved tool observation is fed back into the saved agentic
  messages through `_continue_agentic_answer`. The task is marked `done` only if
  the approved tool and resumed answer both succeed; otherwise it remains
  `blocked`. A second gated action creates a new approval instead of bypassing
  policy.
- Persistence/events: successful resumed steps write a mission memory and emit a
  `mission_step` event, so Command Center refreshes through the existing WS/REST
  flow after `/api/approvals/{id}/execute`.
- Tests: `test_approval_execution_resumes_blocked_mission_step` covers
  block -> approve -> execute approved tool -> resume model -> task done.

## 2026-07-09 handoff — mission approval linkage + retry (track 5.3)

Для оператора и второй модели. Замыкаем петлю approval↔миссия из трека 3/4: когда шаг миссии просит опасный инструмент, создаётся approval и шаг блокируется; не хватало явной связи и способа продолжить.

- Backend: approval, созданный во время шага миссии (`_run_agentic_tool`), теперь несёт `mission_id` в payload и в событии `approval` (парсим из `context.conversation_id`, который для миссий = `mission:{id}`). Аудит/UI могут ассоциировать допуск с миссией. Тест: `test_mission_step_approval_carries_mission_id`.
- Frontend: у заблокированной задачи миссии кнопка меняется на «Повторить» (RefreshCw) — сбрасывает задачу в `pending` (статус разрешён в `MissionTaskUpdateRequest`). Операционная петля: шаг заблокирован → оператор одобряет связанный гейт в панели допусков (виден и по WS) → «Повторить» на задаче → «Запустить всё»/«Шаг» продолжает миссию.
- Полный прогон — 148 pass, ruff clean, frontend typecheck + build clean.
- Осознанно НЕ сделано (глубокая версия, кандидат на будущее): автоматически «скармливать» результат одобренного инструмента в возобновлённый шаг миссии (сейчас повтор шага — свежая агентная попытка; approve+execute гейта выполняет инструмент независимо). Для полноценного resume нужен проброс результата approved-действия в контекст шага.

## 2026-07-09 handoff — live WS events in Command Center (track 5.2)

Для оператора и второй модели. Раньше фронт знал о событиях только через REST-поллинг; серверный event bus (`/ws/events`) во фронте не использовался.

- Frontend подписывается на `ws(s)://<host>:8000/ws/events` (`wsUrl()` = `apiUrl()` с http→ws), автопереподключение через 3s. Агентские события идут как `{channel:"agent", type, title, content, payload}`.
- На событие: пишем в компактную ленту живых событий (`liveEvents`, последние 8) под activity-карточкой + индикатор `liveDot` (пульс когда подключено). На `type` c префиксом `mission` — дебаунс-обновление `/api/missions` (прогресс миссии виден живьём, даже при серверном `/run` или действии из другой вкладки); на `approval` — обновление `/api/approvals`.
- Клиентская цепочка миссий (трек 4) остаётся; теперь её события также приходят по WS, а серверный `/run` тоже даёт живой прогресс в UI.
- Проверка: WS end-to-end через Starlette TestClient (publish в шину → приём на клиенте). Frontend typecheck + build clean.
- Осталось по треку 5: (3) интеграция approval-гейта миссии в поток approvals Command Center (сейчас блокирующий шаг создаёт approval, он виден в панели допусков и по WS; не хватает кнопки «продолжить миссию после approve»).

## 2026-07-09 handoff — semantic hybrid for file chunks (track 5.1)

Для оператора и второй модели. Продолжение трека 2: гибридный retrieval был только для памяти; файловые чанки (`search_file_chunks`) оставались чисто лексическими.

- Рефактор: общий `agent._hybrid_rerank(query, lexical_hits, extra_pool, id_key, limit)` — DRY-ядро фьюза лексики и семантики (RRF) с деградацией. На его основе тонкие `_augment_semantic_memory` (id_key="id", extra = недавние/важные) и новый `_augment_semantic_files` (id_key="chunk_id", extra = oversampled `search_file_chunks(query, 30)`). Оба вызываются в `chat()`/`stream_chat()` после `_prepare_context`.
- Улучшение фьюза: кандидаты переупорядочиваются по семантике перед стабильной сортировкой по fused-скору, поэтому при равном RRF (например, лексика и семантика дали ровно обратные порядки) тай-брейк идёт в сторону семантики — более сильного сигнала для перефразирования. Заодно усиливает и память.
- Ограничение файлового гибрида v1: если лексический поиск не вернул ничего (полное отсутствие пересечения по токенам), пул пуст и семантика не помогает — в отличие от памяти, где есть recent/important пул. Кандидат на будущее: тянуть соседние чанки того же файла или персист векторов чанков.
- Тесты: `test_hybrid_files_reranks_chunks_by_semantic_closeness`. Полный прогон — 147 pass, ruff clean.
- Осталось по треку 5: (2) WS-подписка фронта на `/ws/events` для живых событий миссий/инструментов; (3) интеграция approval-гейта миссии в поток approvals Command Center.

## 2026-07-08 handoff — mission auto-chaining + live progress (track 4)

Для оператора и второй модели. Продолжение трека 3: раньше миссия двигалась по одному шагу за вызов, и «исполнение» было ручным кликом. Теперь миссия может пройти до конца.

- Backend: `agent.run_mission(mission_id, max_steps=None)` последовательно гоняет `execute_next_mission_step` (тот самый агентный executor) до завершения миссии, заблокированного шага (например, нужен approval) или бюджета шагов. Возвращает `MissionRunResponse(mission, steps[], completed, stopped_reason ∈ completed|blocked|budget|empty, executed_steps)` и эмитит событие `mission_run`. Бюджет — из `experience.autonomy_policy.max_autonomous_steps` или явного `max_steps` (cap 24). Approval-гейты НЕ обходятся: заблокированный шаг останавливает цепочку.
- API: `POST /api/missions/{id}/run?max_steps=`. CLI: `mission-run <id> [--max-steps N]`.
- Офлайн детерминирован (каждый шаг — `mission.brief`), поэтому цепочка тестируется без LLM: `test_run_mission_chains_all_steps_offline`, `test_run_mission_respects_step_budget`.
- Frontend (Command Center): кнопка «Запустить всё» в панели миссий делает клиентскую цепочку `execute-next` (через `missionsRef` для свежего состояния между await), чтобы прогресс шёл в UI ЖИВО — прогресс-бар растёт, задачи перекрашиваются в done/blocked, каждый шаг логируется в чат; выполняющаяся миссия подсвечивается, кнопка крутит спиннер. Серверный `/run` остаётся для headless. Причина клиентской цепочки: во фронте нет WS, только REST-поллинг, а per-step `execute-next` даёт живые апдейты без стрима.
- Полный прогон — 146 pass, ruff clean, frontend typecheck + build clean.
- На будущее: WS-подписка на `/ws/events` для полностью серверной цепочки с live-событиями; live-стрим tool-событий шага миссии в UI; интеграция approval-гейта миссии в поток approvals Command Center (сейчас блок останавливает цепочку, approval виден в панели approvals).

## 2026-07-08 handoff — real mission executor (track 3/3)

Для оператора и второй модели. Трек 3 из 3: `execute_next_mission_step` был заглушкой — гонял `mission.brief` (текстовую рекомендацию), а не работу. «Миссии» были планами, которые ничего не делали. Размер модели это не лечит: исполнитель был пустым.

- `agent._execute_mission_step_agentic(mission, task)`: шаг миссии теперь исполняется через агентный tool-loop (`_agentic_answer`) — модель реально вызывает безопасные инструменты (собрать данные, проверить систему, прочитать файлы), опасные становятся approval-гейтами, внутренние tool-runs пишутся в аудит → у миссии появляется настоящий след исполнения. Результат синтезируется в `ToolRunResponse(tool="mission.execute_next", ok, summary=отчёт, data={tool_steps, autonomous})`. Промпт — `MISSION_EXECUTOR_PROMPT` (исполняй шаг, не пиши план).
- Ветвление в `execute_next_mission_step`: при `llm_enabled` — агентное исполнение; при выключенном LLM — прежний `mission.brief` (офлайн-контракт и тест `test_agent_executes_next_mission_step` сохранены, `runs[0]=="mission.brief"`).
- Тест: `test_mission_step_executes_with_tools_when_llm_enabled` (в `test_agentic_loop.py`). Полный прогон — 144 pass, ruff clean.
- Итог трёх треков: (1) у модели появились руки (tool-loop), (2) память стала находить релевантное при перефразировании (гибридный retrieval), (3) миссии реально исполняются. Все три — про архитектуру, а не про веса модели.
- На будущее: авто-цепочка шагов миссии (сейчас один шаг за вызов execute-next), UI-прогресс исполнения в реальном времени, и связка mission-executor с operator-approval потоком в Command Center.

## 2026-07-08 handoff — hybrid semantic memory (track 2/3)

Для оператора и второй модели. Трек 2 из 3: retrieval был чисто лексическим (FTS5 BM25 + LIKE) — перефразированные/иначе склонённые записи не находились, и модель не получала контекст, который «должна была вспомнить». Размер модели это не лечит: retrieval — отдельная подсистема.

- Новый модуль `backend/src/jarvis_gpt/embeddings.py`: `lexical_vector` (чистый Python: слова + символьные триграммы, L2-норма — ловит морфологию/опечатки/порядок слов, которые keyword-поиск упускает), `sparse_cosine`/`dense_cosine`, `reciprocal_rank_fusion`, `EmbeddingBackend` (опциональный OpenAI-совместимый `/embeddings`, при недоступности → None), `semantic_similarity_order` (dense при наличии, иначе lexical).
- Интеграция в `agent.py`: `_augment_semantic_memory(context, message)` вызывается в `chat()`/`stream_chat()` сразу после `_prepare_context`. Берёт пул кандидатов (лексические хиты + недавние/важные из `search_memory(None, 60)`), считает семантический порядок и фьюзит с лексическим через RRF, переписывает `context.memory_hits` (top-8) и проставляет `relevance`/`retrieval="hybrid"`. Пул ограничен → опциональный remote-embed это ОДИН батч-запрос на ход, без персиста векторов и без изменения схемы/пути записи.
- Деградация: пул < 2 → no-op (поэтому все прежние тесты с 1 записью памяти не меняются); любой сбой эмбеддинга → лексический порядок; всё в try/except, ход не ломается.
- Конфиг (новые env, дефолт выключено): `JARVIS_EMBEDDINGS_ENABLED` (false), `JARVIS_EMBEDDINGS_BASE_URL` (по умолчанию = LLM base url), `JARVIS_EMBEDDINGS_MODEL` (пусто). Пока не задан model — работает чистый Python гибрид (уже лучше keyword). Для настоящей семантики укажи локальный embeddings-эндпоинт (llama.cpp/TEI/vLLM-embed).
- Не сделано в этом треке (кандидаты на будущее): гибрид для file_chunks (сейчас только память), персист векторов для больших корпусов вместо ре-эмбеддинга пула на каждый запрос.
- Тесты: `backend/tests/test_embeddings.py` (5). Полный прогон — 143 pass, ruff clean.

## 2026-07-08 handoff — agentic tool loop (track 1/3)

Для оператора и второй модели. Часть плана «убрать узкие места, которые не лечит размер модели». Трек 1 из 3: дать модели реальные руки.

- Было: путь ответа LLM в `chat()`/`stream_chat()` — один forward-pass без доступа к инструментам; всё tool-использование решалось эвристиками ДО модели. Теперь модель сама вызывает инструменты в цикле, видит результат и продолжает.
- Протокол — **JSON-act поверх обычных completions** (деградирует на любой модели, не требует нативного OpenAI tool-calling): модель возвращает `{"tool": "<имя>", "arguments": {...}}` одной строкой → выполняем → возвращаем observation → повтор, пока не хватит, затем финальный текст.
- Безопасность: автономно предлагаются только `danger_level == "safe"` инструменты МИНУС мутирующие (`AGENTIC_TOOL_DENYLIST = memory.save, learning.tick, mission.brief`). Если модель просит review/danger инструмент — создаётся HITL-approval gate (`storage.create_approval`) и в observation уходит «нужно подтверждение», инструмент НЕ выполняется. Бюджет шагов — из `experience.autonomy_policy.max_autonomous_steps` (bounded 1..8, дефолт 4); при исчерпании форсируется финальный ответ (`FINAL_ANSWER_PROMPT`).
- Ключевые части в `agent.py`: `_autonomous_tools()`, `_max_tool_steps()`, `_run_agentic_tool()`, `_agentic_answer()` (non-stream), стрим-версия внутри `stream_chat` через `_ToolActionSniffer` (классифицирует поток как tool-JSON или обычный ответ, чтобы обычные ответы стримились токен-за-токеном без лишнего вызова, а tool-JSON не утекал оператору). Хелперы: `_tool_protocol_prompt`, `_schema_hint`, `_parse_tool_action` (требует, чтобы сообщение НАЧИНАЛОСЬ с JSON — иначе это обычный ответ), `_tool_observation_excerpt`.
- Офлайн/деградация: `_autonomous_tools()` возвращает `[]` при `llm_enabled == False` → путь идентичен прежнему одиночному completion → все офлайн-тесты неизменны. Арбитр интентов (reasoning-first) вызывается только для web_research-планов и кэшируется, так что двойных вызовов роутера нет.
- Тесты: `backend/tests/test_agentic_loop.py` (5): safe-tool→observation→ответ; danger-tool→approval без выполнения; step-budget→форс-финал; стрим подавляет tool-JSON и стримит ответ; обычный стрим без регресса. Полный прогон — 138 pass, ruff clean.
- На будущее по треку: при thinking_enabled модель, обернувшая tool-JSON в `<think>`, классифицируется как ответ (JSON может утечь) — сознательный компромисс v1. Ещё не сделано: трек 2 (семантическая память) и трек 3 (реальный mission-executor поверх этого loop).

## 2026-07-08 handoff — operator persona layer

Для оператора и для второй модели (кто продолжит работу).

- Добавлен слой **operator persona** — durable структурированный профиль оператора, который агент читает на каждом ходу. Цель: закрыть «понимание оператора» широко, а не патчить каждый юзкейс отдельной эвристикой.
- Новый модуль `backend/src/jarvis_gpt/persona.py`: схема + нормализация (`normalize_persona`, `load_persona`), рендер системного блока (`render_system_block`), аксессоры (`home_location`, `primary_language`, `is_configured`) и `PersonaManager` (update/insight с audit + event).
- Поля persona: `display_name, headline, role, location, timezone, languages, expertise, tech_stack, interests, current_focus, standing_instructions, glossary, notes`. Хранится в runtime_kv под ключом `experience.persona`.
- Интеграция в `agent.py`: `_build_llm_messages` подмешивает блок persona; `_infer_weather_location` теперь СНАЧАЛА берёт `persona.location` (обобщение прежнего weather-only кэша — домашний город стал общим фактом для погоды/локальных/гео запросов); добавлены `_persona_prompt`, `_operator_home_location`.
- API: `GET/PATCH /api/persona`, `POST /api/persona/insight` (доклеивание одного факта в list-поле, с дедупом). CLI: `persona`, `persona-set --set key=value`.
- Command Center: в панели «Настройки» добавлена секция «Профиль оператора» (`personaForm`).
- `experience.daily_briefing` выносит `current_focus` оператора в начало focus-списка.
- Тесты: `backend/tests/test_persona.py` (9). Полный прогон — 131 pass, ruff clean, frontend typecheck + build clean.
- Незакрытое/на будущее: авто-обучение persona из диалога (сейчас `add_insight` есть, но агент его из чата не вызывает — сознательно, чтобы не плодить regex-эвристики); можно добавить UI для `glossary` и `languages`, и связать persona.primary_language с языком ответа.

## 2026-07-08 handoff — reasoning-first intent understanding

Для оператора и второй модели. Цель правки: JARVIS должен ПОНИМАТЬ входящую задачу и рассуждать по контексту, а не проходить каскад `_looks_like_*`-затычек.

- Раньше семантический роутер вызывался только в узкой калитке `_should_use_semantic_router` (когда эвристика уже выбрала `web_research` И совпали маркеры) и работал лишь как вето в research-ветке. Это и была корневая «затычечность».
- Теперь в `agent.py` есть **reasoning-first арбитр** `_understand_intent(message, context)`: он вызывается для всей fuzzy web-семьи (гейт — `task_plan.route == "web_research"`, куда эвристика и так сводит weather/shopping/travel/place/osint/generic-research), обогащён operator-контекстом (`_intent_operator_context`: role, home_location, tech_stack, interests) и решает по смыслу: `reasoning|chat|web_research|local_action|mission`.
- Место вызова: `_try_direct_action`, ПОСЛЕ детерминированных fast-path (native OS action, host command, URL) и ПЕРЕД fuzzy-ветками. Если арбитр уверенно (`confidence >= 0.6`) говорит `reasoning`/`chat` — возвращаем None, и основной LLM отвечает рассуждением; при этом `context.task_plan` переписывается `_reroute_plan(...)`, чтобы промпт был когерентным (не «execution contract web_research»). Решение кэшируется на context (`intent_consulted`/`intent_decision`) — ровно один вызов роутера за ход.
- Детерминированные fast-path и офлайн-режим не тронуты: арбитр гейтится на `settings.llm_enabled`, поэтому при выключенном LLM эвристики остаются авторитетом (все офлайн-тесты неизменны).
- Удалён мёртвый `_should_use_semantic_router` (узкая калитка). `_intent_router_messages` переписан в reasoning-first формулировку (сохранена подстрока `intent-router`, которую пинят тесты).
- Промпты: SYSTEM_PROMPT теперь начинается с «сначала пойми задачу и рассуждай по контексту; правила — умолчания, а не скрипт»; task-kernel prompt смягчён с «execution contract» на «стартовая гипотеза, следуй задаче, а не ярлыку».
- Тесты: `test_reasoning_arbiter_can_override_shopping_keyword_plug` (арбитр переопределяет shopping-затычку в reasoning — старая калитка это исключала) и `test_intent_router_receives_operator_persona_context`. Оба пина роутера сохранены. Полный прогон — 133 pass, ruff clean.
- На будущее: арбитр пока не управляет mission-детекцией (`_looks_like_mission` по счётчику ключевых слов) и native/local_action — они детерминированы и покрыты тестами; при желании их тоже можно перевести на понимание.

## 2026-07-08 handoff

- Default runtime is now `gemma4-turbo` / `gemma4-26b-a4b-nvfp4`.
- `gemma4-31b-it-nvfp4` remains in the catalog, but it currently exhausts available KV cache memory at the 32k context target after loading the weights.
- Dispatcher stability flags are pinned for Docker Desktop on Windows: `VLLM_USE_V2_MODEL_RUNNER=0`, `VLLM_WEIGHT_OFFLOADING_DISABLE_UVA=1`, `JARVIS_QWEN_TOKENIZER_MODE=slow`, `JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY=prefetch`.
- Verified tonight: backend `pytest`, `ruff`, frontend `typecheck`, frontend `build`.
- Follow-up closed: `/api/chat/stream` now streams NDJSON deltas and the default generation budget is 512 tokens.
- HITL follow-up closed: approved gates can now be executed through the whitelisted approval executor.
- Conversation history is now durable through `/api/conversations` and can be restored in Command Center.
- Host bridge follow-up closed: bundled `scripts/windows_rpc_bridge.py` exposes local token-auth command execution for approved host actions.
- Autonomous supervisor now persists health snapshots on its own interval, so `/api/status` stays fresh without manual diagnostics.

## Переменные окружения

| Variable | Default | Purpose |
| --- | --- | --- |
| `JARVIS_HOME` | `D:\jarvis` | Внешний runtime root для моделей, кэша, БД и логов |
| `JARVIS_PROFILE` | `gemma4-turbo` | Активный профиль |
| `JARVIS_MODEL_ROOT` | `D:\jarvis\data\models` если существует, иначе `D:\jarvis\models` | Root локальных моделей |
| `JARVIS_LLM_BASE_URL` | `http://localhost:8001/v1` | OpenAI-compatible endpoint |
| `JARVIS_LLM_MODEL` | `dispatcher` | Имя модели для chat completions |
| `JARVIS_LLM_ENABLED` | `1` | Включить/выключить LLM route |
| `JARVIS_EMBEDDINGS_ENABLED` | `0` | Включить remote-эмбеддинги для гибридного retrieval |
| `JARVIS_EMBEDDINGS_BASE_URL` | `= JARVIS_LLM_BASE_URL` | OpenAI-совместимый `/embeddings` endpoint |
| `JARVIS_EMBEDDINGS_MODEL` | `` | Имя embeddings-модели (пусто = только чистый Python гибрид) |
| `JARVIS_AUTONOMY_ENABLED` | `1` | Включить безопасный фоновой supervisor |
| `JARVIS_TELEMETRY_INTERVAL_SEC` | `120` | Интервал telemetry snapshots |
| `JARVIS_HEALTH_INTERVAL_SEC` | `300` | Интервал автономных health snapshots |
| `JARVIS_LEARNING_INTERVAL_SEC` | `600` | Интервал autonomous learning tick |
| `JARVIS_API_HOST` | `0.0.0.0` | Host FastAPI backend |
| `JARVIS_API_PORT` | `8000` | Port FastAPI backend |

## CLI

```powershell
py -3.11 .\jarvis.py init
py -3.11 .\jarvis.py profiles
py -3.11 .\jarvis.py status
py -3.11 .\jarvis.py models
py -3.11 .\jarvis.py models --env
py -3.11 .\jarvis.py llm-health
py -3.11 .\jarvis.py dispatcher-status
py -3.11 .\jarvis.py dispatcher-compose --env
py -3.11 .\jarvis.py dispatcher-up
py -3.11 .\jarvis.py dispatcher-down
py -3.11 .\jarvis.py telemetry --persist
py -3.11 .\jarvis.py host-bridge
py -3.11 .\scripts\windows_rpc_bridge.py
py -3.11 .\jarvis.py host-bridge-exec "Get-Date"
py -3.11 .\jarvis.py autonomy
py -3.11 .\jarvis.py persona
py -3.11 .\jarvis.py persona-set --set location=Kazan --set tech_stack=Proxmox,Debian
py -3.11 .\jarvis.py learning-tick
py -3.11 .\jarvis.py diag
py -3.11 .\jarvis.py chat "JARVIS, оформи это как mission plan: ..."
py -3.11 .\jarvis.py tools
py -3.11 .\jarvis.py tool-run memory.search --set query=runtime --set limit=5
py -3.11 .\jarvis.py ingest README.md
py -3.11 .\jarvis.py files
py -3.11 .\jarvis.py file-search Jarvis --limit 5
py -3.11 .\jarvis.py audit
py -3.11 .\jarvis.py approvals
py -3.11 .\jarvis.py approval-request "Host action" "Needs review" --risk danger
py -3.11 .\jarvis.py approval-update <approval_id> --status approved
py -3.11 .\jarvis.py approval-execute <approval_id>
py -3.11 .\jarvis.py mission-next <mission_id>
py -3.11 .\jarvis.py mission-run <mission_id> --max-steps 8
py -3.11 .\jarvis.py serve --reload
.\scripts\doctor.ps1
```

## API

```text
GET  /health
GET  /api/status
GET  /api/models
GET  /api/dispatcher
POST /api/dispatcher/start
POST /api/dispatcher/stop
GET  /api/telemetry
GET  /api/host-bridge
GET  /api/autonomy
GET  /api/persona
PATCH /api/persona
POST /api/persona/insight
POST /api/learning/tick
POST /api/chat
POST /api/chat/stream
GET  /api/conversations
GET  /api/conversations/{conversation_id}/messages
GET  /api/missions
POST /api/missions
POST /api/missions/{mission_id}/execute-next
POST /api/missions/{mission_id}/run
PATCH /api/missions/{mission_id}/tasks/{task_id}
GET  /api/memory
POST /api/memory
GET  /api/files
POST /api/files/upload
GET  /api/files/search
GET  /api/files/{file_id}
GET  /api/audit
GET  /api/approvals
POST /api/approvals
PATCH /api/approvals/{approval_id}
POST /api/approvals/{approval_id}/execute
GET  /api/tools
POST /api/tools/{tool_name}/run
GET  /api/tool-runs
POST /api/diagnostics
WS   /ws/events
```

## Host Bridge

`scripts/windows_rpc_bridge.py` is a local-only bridge for Windows host actions. It binds to `127.0.0.1:8765`, creates or reads `D:\jarvis\.jarvis\bridge.token`, exposes unauthenticated `/health`, and requires `Authorization: Bearer <token>` for `/execute`.

The normal safe path is:

```powershell
py -3.11 .\scripts\windows_rpc_bridge.py
py -3.11 .\jarvis.py approval-request "Host command" "Run approved local command" --action tool.run --risk danger --payload "{\"tool\":\"host.bridge.execute\",\"arguments\":{\"command\":\"Get-Date\"}}"
py -3.11 .\jarvis.py approval-update <approval_id> --status approved
py -3.11 .\jarvis.py approval-execute <approval_id>
```

For manual diagnostics, `host-bridge-exec` calls the same token-auth bridge directly.

## Storage

SQLite хранится в:

```text
D:\jarvis\data\jarvis-gpt\state\jarvis.sqlite3
```

Файлы, загруженные через Command Center или CLI, копируются в:

```text
D:\jarvis\data\jarvis-gpt\files
```

Активные модели по умолчанию ищутся в:

```text
D:\jarvis\data\models
```

`gemma4-mono` указывает на `gemma4-31b-it-nvfp4`, `gemma4-turbo` — на `gemma4-26b-a4b-nvfp4`. Команда `models --env` печатает переменные для OpenAI-compatible vLLM dispatcher.

Dispatcher запускается отдельно, чтобы не грузить GPU при обычном старте Command Center:

```powershell
.\scripts\dispatcher.ps1 up
.\scripts\dispatcher.ps1 status
.\scripts\dispatcher.ps1 logs
```

Сейчас схема покрывает:

- `conversations`
- `messages`
- `memories`
- `missions`
- `mission_tasks`
- `files`
- `file_chunks`
- `runtime_events`
- `health_snapshots`
- `tool_runs`
- `approvals`
- `telemetry_snapshots`
- `audit_log`

Если SQLite собран с FTS5, память индексируется в `memories_fts`, а файловые чанки — в `file_chunks_fts`. Если FTS5 нет, поиск автоматически деградирует до `LIKE`.
