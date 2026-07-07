# JARVIS GPT

Новая архитектура JARVIS: локальный автономный AI-агент для Windows 11 + WSL2 + Docker.

## Vision

JARVIS GPT — это не чат над LLM. Это локальная агентская операционная система:

- единое ядро агента;
- локальные модели из `D:\jarvis\models`;
- offline-first runtime;
- native Windows control;
- память и знания;
- mission execution;
- самодиагностика.

## Runtime profiles

Поддерживаются только два пользовательских профиля:

- `gemma4-mono` — стабильный режим;
- `gemma4-turbo` — максимальная производительность.

Модели и тяжёлые данные находятся вне репозитория:

```
D:\jarvis\
├── models\
├── cache\
├── data\
├── logs\
└── docker\
```

## Architecture

```
Windows Host
  └── Native Bridge

Docker / WSL2
  ├── Agent Core
  ├── LLM Router
  ├── Memory System
  ├── Tools Runtime
  ├── Diagnostics
  └── API

Frontend
  └── Jarvis Command Center
```

## Status

Repository bootstrap phase.

The system is being rebuilt from scratch with lessons extracted from the original JARVIS-OS project.
