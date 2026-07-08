# Runtime

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
