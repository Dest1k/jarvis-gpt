# Runtime

## 2026-07-08 handoff

- Default runtime is now `gemma4-turbo` / `gemma4-26b-a4b-nvfp4`.
- `gemma4-31b-it-nvfp4` remains in the catalog, but it currently exhausts available KV cache memory at the 32k context target after loading the weights.
- Dispatcher stability flags are pinned for Docker Desktop on Windows: `VLLM_USE_V2_MODEL_RUNNER=0`, `VLLM_WEIGHT_OFFLOADING_DISABLE_UVA=1`, `JARVIS_QWEN_TOKENIZER_MODE=slow`, `JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY=prefetch`.
- Verified tonight: backend `pytest`, `ruff`, frontend `typecheck`, frontend `build`.
- First follow-up: tune `/api/chat` for lower-latency local inference, likely streaming plus smaller default generation budgets.

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
py -3.11 .\jarvis.py autonomy
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
GET  /api/telemetry
GET  /api/host-bridge
GET  /api/autonomy
POST /api/learning/tick
POST /api/chat
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
GET  /api/tools
POST /api/tools/{tool_name}/run
GET  /api/tool-runs
POST /api/diagnostics
WS   /ws/events
```

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
