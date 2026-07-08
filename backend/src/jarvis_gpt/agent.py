from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .config import JarvisSettings
from .event_bus import EventBus
from .llm import LLMRouter
from .models import (
    ChatEvent,
    ChatResponse,
    Mission,
    MissionExecutionResponse,
    MissionTask,
    ToolRunResponse,
)
from .storage import JarvisStorage
from .tools import ToolRegistry

SYSTEM_PROMPT = """Ты JARVIS GPT: локальный агент Windows/WSL/Docker.
Говори по-русски, действуй как инженерный помощник, отделяй факты от предположений.
Тяжелые локальные модели, кеши, данные и логи находятся вне репозитория в D:\\jarvis.
Если локальная LLM или инструмент недоступны, честно называй деградацию
и предлагай следующий проверяемый шаг."""


MISSION_MARKERS = (
    "мисси",
    "mission",
    "план",
    "проект",
    "с нуля",
    "полностью",
    "архитектур",
    "переосмысл",
    "реализ",
)


@dataclass
class AgentContext:
    conversation_id: str
    memory_hits: list[dict[str, Any]]
    file_hits: list[dict[str, Any]]


class AgentRuntime:
    def __init__(
        self,
        *,
        settings: JarvisSettings,
        storage: JarvisStorage,
        llm: LLMRouter,
        bus: EventBus | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.llm = llm
        self.bus = bus
        self.tools = tools or ToolRegistry(settings, storage, llm)

    async def chat(
        self,
        message: str,
        conversation_id: str | None = None,
        mode: str = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        context = self._prepare_context(message, conversation_id)
        events: list[ChatEvent] = [
            ChatEvent(
                type="thought",
                title="Принял задачу",
                content="Определяю режим: короткий ответ, агентский ход или миссия.",
                payload={"profile": self.settings.profile.name},
            )
        ]
        await self._emit(events[-1])

        self.storage.add_message(
            conversation_id=context.conversation_id,
            role="user",
            content=message,
            metadata={"max_tokens": max_tokens, "mode": mode, "temperature": temperature},
        )

        forced_mission = mode == "mission"
        if forced_mission or (mode == "auto" and self._looks_like_mission(message)):
            mission = self.create_mission(message)
            answer = self._mission_answer(mission)
            events.append(
                ChatEvent(
                    type="mission",
                    title="Создан mission plan",
                    content=mission["title"],
                    payload={"mission_id": mission["id"], "tasks": len(mission["tasks"])},
                )
            )
            await self._emit(events[-1])
            message_id = self.storage.add_message(
                conversation_id=context.conversation_id,
                role="assistant",
                content=answer,
                metadata={
                    "mission_id": mission["id"],
                    "events": [event.model_dump() for event in events],
                },
            )
            return ChatResponse(
                conversation_id=context.conversation_id,
                message_id=message_id,
                answer=answer,
                events=events,
                mission_id=mission["id"],
            )

        llm_messages = self._build_llm_messages(context, message)
        events.append(
            ChatEvent(
                type="tool_call",
                title="LLM router",
                content=f"{self.settings.llm_model} через {self.settings.llm_base_url}",
                payload={"enabled": self.settings.llm_enabled},
            )
        )
        await self._emit(events[-1])
        result = await self.llm.complete(
            llm_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if result.ok and result.content:
            answer = result.content
            events.append(
                ChatEvent(
                    type="assistant_done",
                    title="Ответ получен",
                    payload={"source": "llm"},
                )
            )
        else:
            answer = self._offline_answer(message, result.error)
            events.append(
                ChatEvent(
                    type="assistant_done",
                    title="Offline fallback",
                    content=result.error,
                    payload={"source": "fallback"},
                )
            )
        await self._emit(events[-1])
        message_id = self.storage.add_message(
            conversation_id=context.conversation_id,
            role="assistant",
            content=answer,
            metadata={"events": [event.model_dump() for event in events]},
        )
        return ChatResponse(
            conversation_id=context.conversation_id,
            message_id=message_id,
            answer=answer,
            events=events,
        )

    async def stream_chat(
        self,
        message: str,
        conversation_id: str | None = None,
        mode: str = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        context = self._prepare_context(message, conversation_id)
        events: list[ChatEvent] = [
            ChatEvent(
                type="thought",
                title="Accepted task",
                content="Selecting chat, agent, or mission route.",
                payload={"profile": self.settings.profile.name},
            )
        ]
        await self._emit(events[-1])
        yield {"type": "meta", "conversation_id": context.conversation_id}
        yield {"type": "event", "event": events[-1].model_dump()}

        self.storage.add_message(
            conversation_id=context.conversation_id,
            role="user",
            content=message,
            metadata={"max_tokens": max_tokens, "mode": mode, "temperature": temperature},
        )

        forced_mission = mode == "mission"
        if forced_mission or (mode == "auto" and self._looks_like_mission(message)):
            mission = self.create_mission(message)
            answer = self._mission_answer(mission)
            events.append(
                ChatEvent(
                    type="mission",
                    title="Mission plan created",
                    content=mission["title"],
                    payload={"mission_id": mission["id"], "tasks": len(mission["tasks"])},
                )
            )
            await self._emit(events[-1])
            yield {"type": "event", "event": events[-1].model_dump()}
            yield {"type": "delta", "content": answer}
            message_id = self.storage.add_message(
                conversation_id=context.conversation_id,
                role="assistant",
                content=answer,
                metadata={
                    "mission_id": mission["id"],
                    "events": [event.model_dump() for event in events],
                },
            )
            yield {
                "type": "done",
                "answer": answer,
                "conversation_id": context.conversation_id,
                "events": [event.model_dump() for event in events],
                "message_id": message_id,
                "mission_id": mission["id"],
            }
            return

        llm_messages = self._build_llm_messages(context, message)
        events.append(
            ChatEvent(
                type="tool_call",
                title="LLM router",
                content=f"{self.settings.llm_model} via {self.settings.llm_base_url}",
                payload={
                    "enabled": self.settings.llm_enabled,
                    "max_tokens": max_tokens or self.settings.llm_max_tokens,
                    "stream": True,
                },
            )
        )
        await self._emit(events[-1])
        yield {"type": "event", "event": events[-1].model_dump()}

        answer_parts: list[str] = []
        stream_error: str | None = None
        async for chunk in self.llm.stream_complete(
            llm_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            if chunk.kind == "delta" and chunk.content:
                answer_parts.append(chunk.content)
                yield {"type": "delta", "content": chunk.content}
            elif chunk.kind == "error":
                stream_error = chunk.error
                break

        if answer_parts:
            answer = "".join(answer_parts).strip()
            if stream_error:
                interruption = f"\n\n[stream interrupted: {stream_error}]"
                answer = f"{answer}{interruption}"
                yield {"type": "delta", "content": interruption}
            events.append(
                ChatEvent(
                    type="assistant_done",
                    title="Streaming answer received",
                    payload={"source": "llm", "stream": True},
                )
            )
        else:
            answer = self._offline_answer(message, stream_error)
            yield {"type": "delta", "content": answer}
            events.append(
                ChatEvent(
                    type="assistant_done",
                    title="Offline fallback",
                    content=stream_error,
                    payload={"source": "fallback", "stream": True},
                )
            )

        await self._emit(events[-1])
        yield {"type": "event", "event": events[-1].model_dump()}
        message_id = self.storage.add_message(
            conversation_id=context.conversation_id,
            role="assistant",
            content=answer,
            metadata={"events": [event.model_dump() for event in events]},
        )
        yield {
            "type": "done",
            "answer": answer,
            "conversation_id": context.conversation_id,
            "events": [event.model_dump() for event in events],
            "message_id": message_id,
        }

    def create_mission(self, goal: str, title: str | None = None) -> dict[str, Any]:
        mission_title = title or self._title_from_goal(goal)
        mission = self.storage.create_mission(
            title=mission_title,
            goal=goal,
            tasks=self._mission_tasks(goal),
        )
        self.storage.add_event(
            kind="mission.created",
            title=mission_title,
            payload={"mission_id": mission["id"], "task_count": len(mission["tasks"])},
        )
        return mission

    async def execute_next_mission_step(self, mission_id: str) -> MissionExecutionResponse:
        mission = self.storage.get_mission(mission_id)
        if mission is None:
            result = ToolRunResponse(
                tool="mission.execute_next",
                ok=False,
                summary="Mission not found.",
                data={"mission_id": mission_id},
            )
            return MissionExecutionResponse(
                mission=_empty_mission(mission_id),
                task=None,
                result=result,
            )

        task = self.storage.next_mission_task(mission_id)
        if task is None:
            result = ToolRunResponse(
                tool="mission.execute_next",
                ok=True,
                summary="No pending mission tasks.",
                data={"mission_id": mission_id},
            )
            return MissionExecutionResponse(
                mission=Mission.model_validate(mission),
                task=None,
                result=result,
            )

        running_task = self.storage.update_mission_task(task["id"], status="running")
        result = await self.tools.run(
            "mission.brief",
            {"goal": mission["goal"], "task_title": task["title"]},
            mission_id=mission_id,
            task_id=task["id"],
        )
        notes = _task_notes_from_result(result)
        final_status = "done" if result.ok else "blocked"
        updated_task = self.storage.update_mission_task(
            task["id"],
            status=final_status,
            notes=notes,
        )
        if result.ok:
            self.storage.add_memory(
                content=f"Mission step completed: {task['title']}. {result.summary}",
                namespace="missions",
                tags=["mission", mission_id, task["id"]],
                importance=0.62,
            )
        refreshed = self.storage.get_mission(mission_id) or mission
        await self._emit(
            ChatEvent(
                type="mission_step",
                title="Mission step executed",
                content=task["title"],
                payload={"mission_id": mission_id, "task_id": task["id"], "ok": result.ok},
            )
        )
        return MissionExecutionResponse(
            mission=Mission.model_validate(refreshed),
            task=MissionTask.model_validate(updated_task or running_task or task),
            result=result,
        )

    def _prepare_context(self, message: str, conversation_id: str | None) -> AgentContext:
        if conversation_id is None:
            conversation_id = self.storage.create_conversation(self._title_from_goal(message))
        memory_hits = self.storage.search_memory(message[:120], limit=5)
        file_hits = self.storage.search_file_chunks(message[:160], limit=5)
        return AgentContext(
            conversation_id=conversation_id,
            memory_hits=memory_hits,
            file_hits=file_hits,
        )

    def _build_llm_messages(self, context: AgentContext, message: str) -> list[dict[str, str]]:
        memory_block = ""
        if context.memory_hits:
            lines = [
                f"- [{_context_relevance(item)}] {_context_snippet(item)}"
                for item in context.memory_hits[:5]
            ]
            memory_block = "Память, которая может быть полезна:\n" + "\n".join(lines)
        file_block = ""
        if context.file_hits:
            lines = [
                (
                    f"- [{_context_relevance(item)}] "
                    f"{item['file_name']}#{item['position']}: {_context_snippet(item, 900)}"
                )
                for item in context.file_hits[:5]
            ]
            file_block = "Индексированные файлы, которые могут быть полезны:\n" + "\n".join(lines)

        recent = self.storage.recent_messages(context.conversation_id, limit=12)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        operator_prompt = self._operator_prompt()
        if operator_prompt:
            messages.append({"role": "system", "content": operator_prompt})
        if memory_block:
            messages.append({"role": "system", "content": memory_block})
        if file_block:
            messages.append({"role": "system", "content": file_block})
        for item in recent:
            if item["role"] in {"user", "assistant"}:
                messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": message})
        return messages

    def _operator_prompt(self) -> str:
        preferences = self.storage.get_runtime_value("experience.preferences", {})
        if not isinstance(preferences, dict):
            return ""
        operator_name = str(preferences.get("operator_name") or "Admin")[:80]
        style = str(preferences.get("communication_style") or "concise")
        quiet_hours = str(preferences.get("quiet_hours") or "")[:80]
        style_rules = {
            "concise": (
                "Keep answers compact and action-oriented unless the operator asks for detail."
            ),
            "balanced": "Give enough context for decisions, then move to concrete next actions.",
            "detailed": "Explain reasoning, trade-offs and verification steps more fully.",
        }
        return "\n".join(
            [
                "Operator preferences:",
                f"- operator_name: {operator_name}",
                f"- communication_style: {style}",
                f"- quiet_hours: {quiet_hours or 'none'}",
                f"- style_rule: {style_rules.get(style, style_rules['concise'])}",
            ]
        )

    @staticmethod
    def _looks_like_mission(message: str) -> bool:
        normalized = message.lower()
        if "mission plan" in normalized:
            return True
        marker_count = sum(1 for marker in MISSION_MARKERS if marker in normalized)
        return marker_count >= 2 or (len(message) > 320 and marker_count >= 1)

    @staticmethod
    def _title_from_goal(goal: str) -> str:
        cleaned = re.sub(r"\s+", " ", goal).strip()
        cleaned = cleaned.strip(" .,!?:;")
        if not cleaned:
            return "Новая миссия"
        return cleaned[:96] + ("..." if len(cleaned) > 96 else "")

    @staticmethod
    def _mission_tasks(goal: str) -> list[str]:
        normalized = goal.lower()
        tasks = [
            "Зафиксировать цель, границы автономии и ожидаемый результат",
            "Собрать контекст: код, окружение, ограничения и доступные локальные ресурсы",
            "Разложить систему на runtime, память, инструменты, интерфейс и диагностику",
        ]
        if _contains_any(normalized, ("ui", "web", "интерфейс", "command center", "frontend")):
            tasks.append(
                "Спроектировать удобный Command Center: основные панели, состояния, "
                "управление и адаптивность"
            )
        if _contains_any(normalized, ("llm", "модель", "model", "gemma", "dispatcher", "vllm")):
            tasks.append(
                "Проверить LLM-маршрут, модельный профиль, streaming, лимиты токенов "
                "и деградацию без модели"
            )
        if _contains_any(normalized, ("docker", "compose", "контейнер", "gpu", "vram")):
            tasks.append(
                "Стабилизировать Docker/GPU runtime: профили, health checks, логи "
                "и повторяемый запуск"
            )
        if _contains_any(normalized, ("host", "bridge", "windows", "машин", "powershell")):
            tasks.append(
                "Подключить host bridge через token-auth и HITL-gates для опасных "
                "локальных действий"
            )
        if _contains_any(normalized, ("производ", "performance", "быстр", "ресурс", "утилиз")):
            tasks.append(
                "Снять performance-профиль и настроить использование CPU/RAM/GPU "
                "без лишнего давления на систему"
            )
        tasks.extend(
            [
                "Реализовать минимальный рабочий вертикальный срез",
                "Подключить проверки, health-снимки и журнал решений",
                "Провести верификацию, обновить документацию и оформить следующий исполнимый шаг",
            ]
        )
        return _dedupe(tasks)

    @staticmethod
    def _mission_answer(mission: dict[str, Any]) -> str:
        tasks = "\n".join(
            f"{task['position']}. {task['title']}" for task in mission.get("tasks", [])
        )
        return (
            f"Создал mission plan: {mission['title']}\n\n"
            f"{tasks}\n\n"
            "Следующий ход: выполнить первый runnable-шаг и записать результат в журнал."
        )

    @staticmethod
    def _offline_answer(message: str, error: str | None) -> str:
        detail = f" Причина: {error}" if error else ""
        return (
            "Я сейчас работаю в offline-first fallback: backend жив, память и миссии доступны, "
            f"но локальный LLM-router не ответил.{detail}\n\n"
            "Я сохранил твой запрос и могу разложить его как mission plan, либо продолжить после "
            "запуска OpenAI-compatible endpoint на `JARVIS_LLM_BASE_URL`."
        )

    async def _emit(self, event: ChatEvent) -> None:
        self.storage.add_event(kind=f"agent.{event.type}", title=event.title, payload=event.payload)
        if self.bus is not None:
            await self.bus.publish({"channel": "agent", **event.model_dump()})


def _context_snippet(item: dict[str, Any], max_chars: int = 700) -> str:
    value = item.get("snippet") or item.get("content") or ""
    text = " ".join(str(value).split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _context_relevance(item: dict[str, Any]) -> str:
    try:
        relevance = float(item.get("relevance") or 0)
    except (TypeError, ValueError):
        relevance = 0
    return f"{max(0.0, min(1.0, relevance)):.2f}"


def _task_notes_from_result(result: ToolRunResponse) -> str:
    if result.ok:
        action = result.data.get("recommended_action")
        if not action and isinstance(result.data.get("recommended_action"), str):
            action = result.data["recommended_action"]
        if not action and isinstance(result.data, dict):
            action = result.data.get("recommended_action")
        action_text = f"\nRecommended action: {action}" if action else ""
        return f"{result.summary}{action_text}"
    return f"Blocked by tool result: {result.summary}"


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _empty_mission(mission_id: str) -> Mission:
    return Mission(
        id=mission_id,
        title="Missing mission",
        goal="",
        status="blocked",
        progress=0,
        created_at="",
        updated_at="",
        tasks=[],
    )
