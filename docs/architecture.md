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
  |-- conversation context
  |-- mission planner
  |-- memory lookup
  |-- diagnostics
  |-- event stream
  |
LLM Router
  |
OpenAI-compatible Gemma dispatcher

External host runtime
  D:\jarvis\models
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

## Runtime profiles

`gemma4-mono`:

- надёжный cold-start;
- eager mode;
- профиль разработки и диагностики.

`gemma4-turbo`:

- быстрый warmed runtime;
- больше шагов агента;
- включается после проверки окружения.
