# JARVIS GPT

Новая версия JARVIS строится как локальная агентская операционная система, а не как чат-обёртка над LLM. Репозиторий остаётся лёгким и воспроизводимым; модели, базы, кэши и логи живут на машине в `D:\jarvis`.

## Что уже есть

- FastAPI backend с `/health`, `/api/status`, `/api/models`, `/api/chat`, `/api/missions`, `/api/memory`, `/api/files`, `/api/approvals`, `/api/audit`, `/api/diagnostics`.
- Offline-first агент: сохраняет диалоги, создаёт mission plans и деградирует корректно, если локальная LLM не поднята.
- Safe tools runtime: диагностика, статус, память, файловое чтение в разрешённых корнях и execution brief для миссий.
- File ingestion: загрузка текстовых файлов, хранение в `D:\jarvis\data\jarvis-gpt\files`, chunk search и audit trail.
- Model catalog: активные профили знают реальные Gemma 4 веса в `D:\jarvis\data\models`.
- HITL approvals: опасные действия оформляются как durable approval gates, а не выполняются молча.
- Telemetry/performance: CPU/RAM/disk/GPU/Docker snapshots, performance profile и host bridge status.
- Self-learning tick: аудит, tool runs и approvals превращаются в долговременные lessons.
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
py -3.11 .\jarvis.py autonomy
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

## Линия развития

1. Подключить полноценный OpenAI-compatible Gemma dispatcher.
2. Расширить tools runtime: host bridge, sandbox, browser, web, Docker.
3. Развернуть cognitive core: richer project tasks, retrieval ranking, health snapshots.
4. Добавить HITL-gates для опасных действий.
5. Подключить voice/PWA слой после стабилизации текстового runtime.
