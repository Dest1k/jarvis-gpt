# Runtime

## Переменные окружения

| Variable | Default | Purpose |
| --- | --- | --- |
| `JARVIS_HOME` | `D:\jarvis` | Внешний runtime root для моделей, кэша, БД и логов |
| `JARVIS_PROFILE` | `gemma4-mono` | Активный профиль |
| `JARVIS_LLM_BASE_URL` | `http://localhost:8001/v1` | OpenAI-compatible endpoint |
| `JARVIS_LLM_MODEL` | `gemma4-dispatcher` | Имя модели для chat completions |
| `JARVIS_LLM_ENABLED` | `1` | Включить/выключить LLM route |
| `JARVIS_API_HOST` | `0.0.0.0` | Host FastAPI backend |
| `JARVIS_API_PORT` | `8000` | Port FastAPI backend |

## CLI

```powershell
py -3.11 .\jarvis.py init
py -3.11 .\jarvis.py profiles
py -3.11 .\jarvis.py status
py -3.11 .\jarvis.py diag
py -3.11 .\jarvis.py chat "JARVIS, оформи это как mission plan: ..."
py -3.11 .\jarvis.py tools
py -3.11 .\jarvis.py tool-run memory.search --set query=runtime --set limit=5
py -3.11 .\jarvis.py ingest README.md
py -3.11 .\jarvis.py files
py -3.11 .\jarvis.py file-search Jarvis --limit 5
py -3.11 .\jarvis.py audit
py -3.11 .\jarvis.py mission-next <mission_id>
py -3.11 .\jarvis.py serve --reload
```

## API

```text
GET  /health
GET  /api/status
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
- `audit_log`

Если SQLite собран с FTS5, память индексируется в `memories_fts`, а файловые чанки — в `file_chunks_fts`. Если FTS5 нет, поиск автоматически деградирует до `LIKE`.
