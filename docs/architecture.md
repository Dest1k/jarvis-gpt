# JARVIS GPT Architecture

## Design goals

JARVIS GPT is built as a local agent runtime, not a chat wrapper.

## Components

```
User
 |
Command Center
 |
Agent Runtime
 |
+-- Planner
+-- Memory
+-- Tool Registry
+-- Diagnostics
 |
LLM Router
 |
Gemma Mono / Turbo
```

## Storage principle

Large runtime assets live outside git. The host machine provides the storage root with:

- models
- cache
- runtime data
- logs
- docker resources

The repository remains lightweight and reproducible.

## Profiles

Mono:
- reliable baseline
- eager execution
- primary development mode

Turbo:
- performance mode
- optimized execution
- enabled after validation
