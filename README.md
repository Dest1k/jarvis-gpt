# JARVIS GPT

Новая версия JARVIS строится как локальная агентская операционная система, а не как чат-обёртка над LLM. Репозиторий остаётся лёгким и воспроизводимым; модели, базы, кэши и логи живут на машине в `D:\jarvis`.

## Что уже есть

- FastAPI backend с `/health`, `/api/status`, `/api/chat`, `/api/missions`, `/api/memory`, `/api/diagnostics`.
- Offline-first агент: сохраняет диалоги, создаёт mission plans и деградирует корректно, если локальная LLM не поднята.
- SQLite WAL-хранилище в `D:\jarvis\data\jarvis-gpt\state\jarvis.sqlite3`.
- Два runtime-профиля: `gemma4-mono` и `gemma4-turbo`.
- Next.js Command Center: чат, статус runtime, миссии и диагностика.
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
    jarvis-gpt\
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
```

`gemma4-mono` — стабильный baseline для холодного старта и диагностики.

`gemma4-turbo` — быстрый профиль для прогретого runtime.

## Docker

```powershell
$env:JARVIS_HOST_HOME="D:/jarvis"
docker compose up --build
```

## Линия развития

1. Подключить полноценный OpenAI-compatible Gemma dispatcher.
2. Расширить tools runtime: host bridge, sandbox, filesystem, browser, web, Docker.
3. Развернуть cognitive core: audit, RAG-ingestion, project tasks, health snapshots.
4. Добавить HITL-gates для опасных действий.
5. Подключить voice/PWA слой после стабилизации текстового runtime.
