# Agent Core

Рабочее ядро теперь находится в `backend/src/jarvis_gpt`.

Модули:

- `agent.py` — агентский ход, mission-plan эвристика, fallback без LLM.
- `llm.py` — OpenAI-compatible LLM router.
- `storage.py` — SQLite WAL, память, диалоги, миссии, health snapshots.
- `diagnostics.py` — проверки Python, путей, Docker, Git, SQLite и LLM endpoint.
- `api.py` — FastAPI REST/WebSocket поверхность.
- `cli.py` — локальный запуск, диагностика, статус, one-shot chat.

Этот файл оставлен как указатель для старой структуры.
