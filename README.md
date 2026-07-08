# JARVIS GPT

Новая версия JARVIS строится как локальная агентская операционная система, а не как чат-обёртка над LLM. Репозиторий остаётся лёгким и воспроизводимым; модели, базы, кэши и логи живут на машине в `D:\jarvis`.

## Что уже есть

- FastAPI backend с `/health`, `/api/status`, `/api/models`, `/api/chat`, `/api/chat/stream`, `/api/missions`, `/api/memory`, `/api/files`, `/api/approvals`, `/api/audit`, `/api/diagnostics`.
- Offline-first агент: сохраняет диалоги, создаёт mission plans и деградирует корректно, если локальная LLM не поднята.
- Safe tools runtime: диагностика, статус, память, публичный web fetch с SSRF-защитой, approval-gated browser open, Docker ps/logs для Jarvis-контейнеров, файловое чтение в разрешённых корнях, approval-gated sandbox write, token-auth host bridge и execution brief для миссий.
- File ingestion: загрузка текстовых файлов, хранение в `D:\jarvis\data\jarvis-gpt\files`, chunk search и audit trail.
- Model catalog: активные профили знают реальные Gemma 4 веса в `D:\jarvis\data\models`.
- HITL approvals: опасные действия оформляются как durable approval gates, а не выполняются молча.
- Telemetry/performance: CPU/RAM/disk/GPU/Docker snapshots, performance profile и host bridge status.
- Self-learning tick: аудит, tool runs и approvals превращаются в долговременные lessons.
- Operator persona: durable structured profile (роль, домашний город, языки, стек, увлечения, текущий фокус, постоянные правила «всегда/никогда», глоссарий) читается агентом в каждом ответе.
- Reasoning-first понимание задачи: для fuzzy web-запросов агент спрашивает модель (`_understand_intent`), которая понимает интент по смыслу и профилю оператора, а не по ключевым словам; `_looks_like_*`-эвристики остаются детерминированным офлайн-фолбэком.
- Агентный tool-loop: на пути ответа модель сама вызывает безопасные инструменты (web.search/fetch, filesystem/docker/runtime read, ...), видит observation и продолжает до готовности; опасные инструменты уходят в HITL-approval, бюджет шагов — из autonomy policy. Протокол JSON-act поверх обычных completions, деградирует без нативного tool-calling.
- Гибридная семантическая память: retrieval фьюзит лексический BM25/LIKE с семантическим re-ranking (чистый Python fuzzy-вектор по умолчанию, опциональный remote `/embeddings` для настоящей семантики) через RRF, поэтому релевантные записи находятся даже при перефразировании и иной словоформе. Деградирует до лексики без потерь.
- Retrieval adds normalized relevance, matched terms and snippets for memory/file context.
- Learning tick deduplicates repeated lessons before writing long-term memory.
- Autonomous supervisor: безопасный фоновой цикл собирает telemetry и запускает learning tick.
- Исполнение следующего шага mission plan с прогрессом задач и журналом tool runs.
- SQLite WAL-хранилище в `D:\jarvis\data\jarvis-gpt\state\jarvis.sqlite3`.
- Два runtime-профиля: `gemma4-mono` и `gemma4-turbo`.
- Next.js Command Center: чат, статус runtime, миссии и диагностика.
- Command Center показывает файлы, поиск по чанкам, ручную память, tools и audit stream.
- Command Center показывает локальные модели, approvals, активный профиль и dispatcher-конфигурацию.
- Command Center показывает ресурсы, GPU, host bridge и запускает learning tick.
- CLI `py -3.11 .\jarvis.py ...` и Docker Compose для повторяемого запуска.

## Быстрый старт

Unified launcher with keyboard menu:

```powershell
.\jarvis.cmd
```

One-command start/stop/status:

```powershell
.\jarvis.cmd start -Profile gemma4-turbo
.\jarvis.cmd start -Profile gemma4-mono
.\jarvis.cmd stop
.\jarvis.cmd restart -Profile gemma4-turbo
.\jarvis.cmd restart -Profile gemma4-turbo -BuildFrontend
.\jarvis.cmd status
.\jarvis.cmd llm
.\jarvis.cmd llm -WatchLlm
```

The launcher auto-rebuilds the production frontend when `frontend/app`, `public`, config or lock files are newer than `.next/BUILD_ID`. Full-stack start also attempts to start Docker Desktop and waits for the Docker API before starting the LLM dispatcher; use `-NoDockerStart` only for manual Docker diagnostics.

Profile shortcuts:

```powershell
.\jarvis-turbo.cmd
.\jarvis-mono.cmd
.\jarvis-start.cmd -Profile gemma4-turbo
.\jarvis-stop.cmd
```

Manual low-level startup remains available:

```powershell
py -3.11 .\jarvis.py init
py -3.11 .\jarvis.py diag
py -3.11 .\jarvis.py serve --reload
```

В отдельном окне:

```powershell
cd frontend
npm install
npm run dev
```

Command Center:

```text
http://localhost:3000
```

Backend:

```text
http://localhost:8000
```

## Локальные данные

```text
D:\jarvis\
  models\
  cache\
    jarvis-gpt\
  data\
    models\
      gemma4-31b-it-nvfp4\
      gemma4-26b-a4b-nvfp4\
    jarvis-gpt\
      files\
      state\
        jarvis.sqlite3
  logs\
    jarvis-gpt\
  docker\
    jarvis-gpt\
```

## Профили

```powershell
py -3.11 .\jarvis.py profiles
py -3.11 .\jarvis.py --profile gemma4-mono serve
py -3.11 .\jarvis.py --profile gemma4-turbo serve
py -3.11 .\jarvis.py tools
py -3.11 .\jarvis.py models
py -3.11 .\jarvis.py models --env
py -3.11 .\jarvis.py llm-health
py -3.11 .\jarvis.py dispatcher-status
.\scripts\dispatcher.ps1 up
py -3.11 .\jarvis.py telemetry --persist
py -3.11 .\jarvis.py host-bridge
py -3.11 .\scripts\windows_rpc_bridge.py
py -3.11 .\jarvis.py host-bridge-exec "Get-Date"
py -3.11 .\jarvis.py autonomy
py -3.11 .\jarvis.py persona
py -3.11 .\jarvis.py persona-set --set location=Kazan --set tech_stack=Proxmox,Debian
py -3.11 .\jarvis.py learning-tick
py -3.11 .\jarvis.py tool-run memory.search --set query=runtime --set limit=5
py -3.11 .\jarvis.py ingest README.md
py -3.11 .\jarvis.py file-search Jarvis
py -3.11 .\jarvis.py audit
py -3.11 .\jarvis.py approvals
py -3.11 .\jarvis.py approval-request "Host action" "Needs review" --risk danger
py -3.11 .\jarvis.py mission-next <mission_id>
```

Полная локальная проверка:

```powershell
.\scripts\doctor.ps1
```

`gemma4-mono` — стабильный baseline на `gemma4-31b-it-nvfp4` для холодного старта и диагностики.

`gemma4-turbo` — быстрый профиль на `gemma4-26b-a4b-nvfp4` для прогретого runtime.

## Docker

```powershell
$env:JARVIS_HOST_HOME="D:/jarvis"
docker compose up --build
```

LLM dispatcher поднимается отдельным профилем, чтобы тяжёлая модель не стартовала случайно:

```powershell
.\scripts\dispatcher.ps1 up
# или
docker compose --profile llm up -d dispatcher
```

## Current readiness

- Unified launcher `.\jarvis.cmd` provides keyboard-menu start/stop/restart/status/logs/doctor/open flows plus `gemma4-turbo` and `gemma4-mono` startup shortcuts.
- Experience API persists operator preferences, autonomy policy, daily briefing, self-heal reports and benchmark history in SQLite.
- Operator persona (`/api/persona`) is a first-class understanding layer: the agent injects it into every LLM turn, uses `location` as the generic place fallback (weather/local/geo) instead of a weather-only cache, and surfaces `current_focus` in the daily briefing. Editable from Command Center → «Профиль оператора» and via `jarvis persona` / `persona-set`.
- Command Center exposes briefing, autonomy policy modes, self-heal suggestions, benchmark telemetry and operator communication preferences.
- Self-heal scans diagnostics/resources/dispatcher state and proposes safe or approval-gated actions without silently mutating host state.
- Performance benchmark records storage, telemetry, dispatcher and LLM health latency with resource-guard recommendations.
- Browser automation has a persisted policy plus approved multi-tab opening for repeat workflows.
- Docker operations have a persisted Jarvis-container policy and fleet view with allowed/blocked annotations.
- Autonomy jobs can be created, budgeted and run manually from API and Command Center.
- Reusable operator routines run briefing, diagnostics, self-heal, benchmark and learning workflows.
- Directory ingestion indexes larger local text/code trees from allowed roots with file limits.
- OpenAI-compatible Gemma dispatcher is wired and can be started/stopped from CLI, API, and Command Center.
- Chat supports streamed NDJSON deltas through `/api/chat/stream`; the UI renders tokens as they arrive.
- Conversation history is durable and can be restored from Command Center after reload.
- Command Center has browser voice input for the chat composer where the Web Speech API is available.
- Command Center registers a service worker and keeps the local UI shell available after the first successful load.
- Command Center can run safe public web fetches and show the clipped result inline.
- Command Center can create host-command approval gates and execute them after approval.
- Native host bridge now has a bundled local RPC script, token detection, CLI execution, and a `danger` tool for approved host commands.
- Safe tools include `web.fetch` for public HTTP(S) context with private-network and redirect guards.
- Safe tools include read-only `docker.ps` and restricted `docker.logs` for Jarvis container diagnostics.
- Dispatcher status/logs are tools, while dispatcher start/stop are approval-gated tool actions.
- `browser.open` can open validated HTTP(S) URLs through the host bridge after approval.
- `filesystem.write_text` is sandboxed to the repository or `D:\jarvis` and requires approval.
- Memory and file retrieval now return relevance scores, matched terms, and clipped snippets for mission context.
- Autonomous learning skips duplicate lessons and reports the skipped count in tick results.
- Autonomous supervisor persists telemetry, learning lessons, and health snapshots on separate intervals.
- Mission planner enriches plans with domain-specific UI, LLM, Docker/GPU, host bridge and performance steps.
- HITL gates now have a whitelisted executor: approved gates can run dispatcher, diagnostics, learning, telemetry, memory, or registered tool actions.
- Full local smoke covers backend tests/lint/compile, Docker Compose config, frontend audit/typecheck/build, and optional live HTTP checks.

## Closed extension tracks

- Richer browser automation policy beyond approved URL opening.
- Richer Docker policy for multi-container Jarvis deployments.
- Deeper autonomous task scheduling with explicit operator budgets.
- Larger retrieval ingestion pipelines for documents and codebases.
- More Command Center workflows for repeated operator routines.
