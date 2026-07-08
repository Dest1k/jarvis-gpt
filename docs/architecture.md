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
  |-- host bridge status and gated execution
  |-- task lifecycle
  |-- diagnostics
  |-- event stream
  |-- model catalog
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
- Агентный tool-loop: на пути ответа модель — не одиночный forward-pass, а цикл, где она сама выбирает безопасные инструменты, читает observation и продолжает до финального ответа. Опасные инструменты автономно не выполняются, а становятся approval-гейтами. Это снимает «чат-бот»-стену (у модели появляются руки), не завися от размера модели.
- Гибридный retrieval: память достаётся не только лексически (BM25/LIKE), но и семантически (fuzzy-вектор или remote-эмбеддинги), фьюз через RRF над ограниченным пулом кандидатов. Модель получает релевантный контекст даже при перефразировании — это отдельная подсистема, которую нельзя «дообучить» размером чат-модели.
- Опасные действия не входят в safe tools layer; shell/host control идёт через token-auth host bridge и HITL-gates.
- Файлы попадают в runtime-хранилище через upload/CLI, а агент читает только индексированные чанки через safe tools.
- Audit log фиксирует изменения памяти, миссий, task lifecycle, tool runs и ingestion.
- Профили `gemma4-mono` и `gemma4-turbo` указывают на реальные каталоги весов в `D:\jarvis\data\models`; backend не хранит веса в репозитории.
- Dispatcher вынесен в отдельный Compose profile `llm`, чтобы Command Center можно было запускать без случайной загрузки тяжёлых весов в VRAM.
- Любое действие с риском выше safe должно сначала стать approval gate; выполнение после approve проходит через отдельный whitelisted gated executor.
- Self-learning пока идёт через explicit `learning.tick`: он добывает lessons из audit/tool/approval истории и пишет их в долговременную память.
- Autonomous supervisor безопасно выполняет только наблюдение: telemetry snapshots и learning tick по расписанию; действия с риском остаются через approvals.
- Performance слой разделяет лёгкий backend и тяжёлый dispatcher; GPU утилизируется vLLM-профилем, а backend собирает telemetry без удержания весов.

## Runtime profiles

`gemma4-mono`:

- надёжный cold-start;
- eager mode;
- профиль разработки и диагностики.

`gemma4-turbo`:

- быстрый warmed runtime;
- больше шагов агента;
- включается после проверки окружения.
