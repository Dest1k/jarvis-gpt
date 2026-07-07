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
py -3.11 .\jarvis.py serve --reload
```

## API

```text
GET  /health
GET  /api/status
POST /api/chat
GET  /api/missions
POST /api/missions
GET  /api/memory
POST /api/memory
POST /api/diagnostics
WS   /ws/events
```

## Storage

SQLite хранится в:

```text
D:\jarvis\data\jarvis-gpt\state\jarvis.sqlite3
```

Сейчас схема покрывает:

- `conversations`
- `messages`
- `memories`
- `missions`
- `mission_tasks`
- `runtime_events`
- `health_snapshots`
