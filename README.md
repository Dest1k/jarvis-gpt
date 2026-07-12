# JARVIS GPT

Новая версия JARVIS строится как локальная агентская операционная система, а не как чат-обёртка над LLM. Репозиторий остаётся лёгким и воспроизводимым; модели, базы, кэши и логи живут на машине в `D:\jarvis`.

## Что уже есть

- FastAPI backend с `/health`, `/api/status`, `/api/models`, `/api/chat`, `/api/chat/stream`, `/api/missions`, `/api/memory`, `/api/files`, `/api/approvals`, `/api/audit`, `/api/diagnostics`.
- Offline-first агент: сохраняет диалоги, создаёт mission plans и деградирует корректно, если локальная LLM не поднята.
- Safe tools runtime: диагностика, статус, память, публичный web fetch/search/render/download с SSRF-защитой, evidence ledger/extract/verify, schema.org/OpenGraph/readability extraction, quarantine download inspection, semantic review-gated Chrome CDP read/click/type/select/screenshot plus human handoff status, validated browser open без approval для явных запросов открыть URL, Docker ps/logs для Jarvis-контейнеров, файловое чтение в разрешённых корнях, approval-gated sandbox write, token-auth host bridge и execution brief для миссий.
- File ingestion: загрузка текстовых, Word/Excel/PDF файлов, хранение в `D:\jarvis\data\jarvis-gpt\files`, document extraction, chunk search и audit trail.
- Document surfer: изолированный black-box обработчик документов (`document_surfer`, аналог `web_surfer`) — inspect/read/analyze/compare/search/corpus/generate/convert для Word/Excel/PDF/PPTX/текста, copy-on-write правки и генерация md/docx/xlsx без перезаписи оригиналов.
- Model catalog: активные профили знают реальные Gemma 4 веса в `D:\jarvis\data\models`.
- HITL approvals: незапрошенные опасные действия оформляются как durable approval gates; точная
  явная команда текущего сообщения получает одноразовое argument-bound разрешение и выполняется сразу.
- Telemetry/performance: CPU/RAM/disk/GPU/Docker snapshots, performance profile и host bridge status.
- Self-learning tick: аудит, tool runs, approvals и append-only learning journal превращаются в долговременные lessons.
- Operator persona: durable structured profile (роль, домашний город, языки, стек, увлечения, текущий фокус, постоянные правила «всегда/никогда», глоссарий) читается агентом в каждом ответе.
- Persona auto-learning: модель сама сохраняет устойчивые факты об операторе через safe-инструмент `persona.insight` (один факт за вызов, дедуп, капы, аудит) — без regex-извлечения.
- Reasoning-first понимание задачи: для fuzzy web-запросов И для запросов о состоянии/действиях на машине оператора агент спрашивает модель (`_understand_intent`), которая понимает интент по смыслу и профилю оператора, а не по ключевым словам; `_looks_like_*`-эвристики остаются детерминированным офлайн-фолбэком. Арбитр может повышать задачу до миссии и уводить локальные запросы в нативные инструменты: решение `local_action` направляет к system.inspect (чтение состояния) и windows.native (мутации под approval) вместо интернет-поиска локального состояния.
- Web evidence synthesis: `web.answer` is the first-choice Google-like route with optional Search API providers (Brave/Tavily/Serper), vertical search modes, research/fetch/render/archive fallback, verification, grounded LLM synthesis with URL retention, answer TTL cache, source diversity, claim-level citations, transcripts/eval hooks, and structured cards for follow-up/UI use.
- Агентный tool-loop: модель вызывает безопасные инструменты и только те mutating-инструменты,
  которые семантически совпадают с явной текущей командой; остальные уходят в HITL-approval.
- Гибридная семантическая память и файлы: retrieval фьюзит лексический BM25/LIKE с семантическим re-ranking (чистый Python fuzzy-вектор по умолчанию, опциональный remote `/embeddings` для настоящей семантики) через RRF — и для долговременной памяти, и для индексированных файловых чанков — поэтому релевантное находится даже при перефразировании и иной словоформе. Деградирует до лексики без потерь. При полном отсутствии лексического пересечения файловый retrieval падает на ограниченный recent-chunk пул с порогом связности, а не молчит.
- Реальное исполнение миссий: шаг миссии при живом LLM выполняется агентным tool-loop (реальные инструменты, approval для опасного, аудит), а не статичным brief; офлайн — прежний детерминированный brief.
- Авто-цепочка миссий: `run_mission` / `POST /api/missions/{id}/run` / `mission-run` проходят миссию до завершения, блокировки или бюджета шагов; в Command Center кнопка «Запустить всё» показывает прогресс живьём (прогресс-бар, статусы задач, лог шагов).
- Живые события: Command Center подписан на `/ws/events` по WebSocket — лента активности агента, живой индикатор и авто-обновление миссий/допусков без поллинга.
- Per-answer thought trace: у каждого сохранённого ответа есть переход на `/trace/{messageId}` с визуальной цепочкой input -> runtime events -> output.
- Result integrity: substantive-ответы проходят самопроверку против задачи и критериев готовности с одним ремонт-раундом (rewrite в чате, «Поправка после самопроверки» в стриме, переписанный отчёт шага миссии); сбой критика никогда не портит ответ. Выключается `JARVIS_VERIFY_ANSWERS=0` или policy-ключом `verify_answers`.
- Итоговый mission-отчёт: завершённая миссия синтезирует операторский deliverable (LLM + детерминированный fallback), доступный через `final_report`, `GET /api/missions/{id}/report`, событие `mission_report` и память.
- Clarify-маршрут: если задача действительно неоднозначна, арбитр задаёт один точный вопрос вместо уверенной догадки.
- Experience loop: оценки оператора (👍/👎 с комментарием), revise-вердикты самопроверки и отклонённые допуски становятся уроками с реальными цитатами, а топ-уроки вставляются в промпт каждого хода — негативный опыт меняет поведение со следующего ответа. Качество агрегируется в operator queue (kind `quality`).
- Retrieval adds normalized relevance, matched terms and snippets for memory/file context.
- Learning tick deduplicates repeated lessons before writing long-term memory.
- Autonomous supervisor: безопасный фоновой цикл собирает telemetry и запускает learning tick.
- Исполнение следующего шага mission plan с прогрессом задач и журналом tool runs.
- SQLite WAL-хранилище в `D:\jarvis\data\jarvis-gpt\state\jarvis.sqlite3`.
- Runtime-профили под RTX 5090 32GB + 128GB RAM: `gemma4-turbo` (26B fast), `gemma4-mono` (31B partial offload), `gemma4-mono-perf` (31B GPU-first).
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
.\jarvis.cmd app -Profile gemma4-turbo
.\jarvis.cmd stop
.\jarvis.cmd restart -Profile gemma4-turbo
.\jarvis.cmd restart -Profile gemma4-turbo -BuildFrontend
.\jarvis.cmd status
.\jarvis.cmd llm
.\jarvis.cmd llm -WatchLlm
```

The launcher auto-rebuilds the production frontend when `frontend/app`, `public`, config or lock files are newer than `.next/BUILD_ID`. `jarvis.cmd app` starts the bridge, backend, and UI without starting or later stopping the LLM dispatcher. Full-stack start first reuses an already running dispatcher/OpenAI-compatible endpoint; only when no LLM is active does it start Docker Desktop and the dispatcher. Command Center currently opens on localhost without browser login; LAN mode is temporarily disabled. Use `-NoDockerStart` only for manual Docker diagnostics.

Профиль LLM выбирается стрелками в меню `.\jarvis.cmd` (Start / Restart):
Turbo 26B, Mono 31B offload, Mono 31B perf. CLI-флаг `-Profile` остаётся для скриптов.

```powershell
.\jarvis.cmd
.\jarvis.cmd start -Profile gemma4-mono-perf
.\jarvis-stop.cmd
```

Manual low-level startup remains available:

```powershell
py -3.11 -m pip install -r .\backend\requirements.txt
py -3.11 -m playwright install --only-shell chromium
py -3.11 .\jarvis.py init
py -3.11 .\jarvis.py diag
py -3.11 .\jarvis.py serve --reload
```

The Playwright browser install is a one-time prerequisite for the bundled
`web_surfer` black box. Docker images install the same headless Chromium build
at image-build time. If browser provisioning is unavailable, the adapter fails
closed and the existing generic web tools remain usable.

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
py -3.11 .\jarvis.py host-bridge-action window.list --payload-json '{"limit":10}'
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
py -3.11 .\jarvis.py mission-run <mission_id> --max-steps 8
```

Полная локальная проверка:

```powershell
.\scripts\doctor.ps1
```

`gemma4-mono` — 31B IT NVFP4 со partial CPU offload + KV swap (стабильность, cold start).
`gemma4-mono-perf` — тот же 31B, но GPU-first (без offload, CUDA graphs, context 8k) для максимальной скорости.
`gemma4-turbo` — 26B A4B NVFP4, быстрый warmed path без offload.

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

- Internet production surface: `web.answer`, `web.research`, `web.document.read`,
  `internet.search_api.status`, `browser.session.diagnose`,
  `internet.observability`, and `internet.smoke` are safe tools. Command Center
  status shows internet handoff, evidence/research counts, answer cache, Search
  API readiness/stats, recent blocked pages, cooldowns, top domain/provider, and
  can run a smoke check from the web URL draft.
- Internet everyday coverage: `web.archive` читает Wayback-копию заблокированных
  или исчезнувших страниц (blocked-ответ `web.fetch` сам подсказывает этот
  фолбэк), `web.feed` читает RSS/Atom вместо скрейпинга, `web.weather` даёт
  геокодированный прогноз через бесключевой Open-Meteo (погодный маршрут
  пробует его первым и честно падает на поиск), а `web.watch.add/list/remove` +
  фоновый job kind `web.watch` следят за изменением страницы (цена, наличие,
  статус) и при изменении поднимают событие и durable-память.
- Document intelligence surface: uploaded files or local paths can use
  `documents.inspect`, `documents.review`, `documents.read`,
  `documents.compare`, `documents.edit.plan`, and
  `documents.apply_replacements` for Word/Excel/PDF and text-like files.
  `documents.review` reports OCR need, Word redline readiness, and Excel
  formula/style audit. Edited copies are written under `data/document-outputs`
  without overwriting originals.
- Unified launcher `.\jarvis.cmd`: keyboard menu (arrows) to start/stop/restart and pick LLM profile — Turbo 26B, Mono 31B offload, Mono 31B perf.
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
- Command Center can upload Office/PDF/text attachments for document extraction
  and can run safe public web fetches with clipped results inline.
- Command Center can create typed `jarvis.execution.v1` approval gates and execute them after approval.
- Deterministic execution tools provide typed OS actions, rich process feedback, durable rollback checkpoints, cross-restart transaction idempotency, exact process ownership, causal postcondition verification, and bounded session history. Failed startup rollback latches mutations closed; process/network/registry capabilities are deny-by-default.
- Executive missions are persisted as cycle-safe `jarvis.planner.v1` DAGs behind the `jarvis.executive.v1` coordinator. Only dependency-ready tasks can be claimed; every step has assertions, failed branches are revised in place, and completed work survives replanning.
- API and mutating CLI executive operations share a crash-safe `jarvis.primary-runtime-lease.v1` OS lock, preventing cross-process lost updates during planning, approvals, and recovery.
- Independent state verification validates changed files, syntax, TCP listeners, registry values, and process identity before success is committed. Safe Gates classify destructive actions, require a dry-run, and use one-shot action-bound permits for high/critical risk.
- Cold start writes a verified `host_profile.json` and adapts plans to its stable fingerprint. Execution playbooks persist only independently verified typed-action `[symptom -> solution -> verification]` facts in a dedicated SQLite store; LLM reports and retrieved/remote text stay untrusted data.
- The bundled Claude-owned `web_surfer` service is connected only through `jarvis.web-surfer-adapter.v1` and its public async `fast_fact`, `deep_research`, and `aggressive_shopping` methods. Its Playwright/BeautifulSoup/lxml dependencies are pinned, and Docker provisions a matching headless Chromium build. The service runs in a resident process-tree-contained worker with bounded IPC, public-target validation, hard lifetime deadlines, restart after failure, and deterministic shutdown; missing browser provisioning or contract drift fails closed without changing the existing web stack.
- Native host bridge uses token-authenticated `action.v1`; arbitrary command execution is removed, `/execute` returns `410 Gone`, and process launch is restricted to a fixed desktop-app/argument grammar.
- Safe tools include `web.fetch` for public HTTP(S) context with private-network and redirect guards.
- Web tools mark remote content as untrusted evidence, flag prompt-injection markers, and quarantine downloads without auto-opening files.
- Browser tools include local-only Chrome CDP status/launch/read flows for reading pages through a dedicated user browser session without exporting cookies.
- Safe tools include read-only `docker.ps` and restricted `docker.logs` for Jarvis container diagnostics.
- Safe tool `system.inspect` даёт read-only инспекцию машины. Явно названные в текущем сообщении
  native-действия выполняются через `windows.native` сразу; выведенные моделью действия остаются gated.
- Dispatcher status/logs are tools, while dispatcher start/stop are approval-gated tool actions.
- `browser.open` can open validated HTTP(S) URLs through the host bridge without approval for explicit open requests; it is excluded from the autonomous background tool loop.
- `filesystem.write_text` is sandboxed to the repository or `D:\jarvis`; an exact current-turn
  path/content request executes immediately, `mode=create` safely creates empty/new files without
  overwriting, while inferred writes require approval.
- Memory and file retrieval now return relevance scores, matched terms, and clipped snippets for mission context.
- Autonomous learning skips duplicate lessons, reads the durable learning journal, and reports the skipped count in tick results.
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
