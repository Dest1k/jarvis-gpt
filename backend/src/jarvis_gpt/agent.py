from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import JarvisSettings
from .event_bus import EventBus
from .llm import LLMRouter
from .models import ChatEvent, ChatResponse
from .storage import JarvisStorage

SYSTEM_PROMPT = """Ты JARVIS GPT: локальный агент Windows/WSL/Docker.
Говори по-русски, действуй как инженерный помощник, отделяй факты от предположений.
Тяжёлые локальные модели, кэши, данные и логи находятся вне репозитория в D:\\jarvis.
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


class AgentRuntime:
    def __init__(
        self,
        *,
        settings: JarvisSettings,
        storage: JarvisStorage,
        llm: LLMRouter,
        bus: EventBus | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.llm = llm
        self.bus = bus

    async def chat(
        self,
        message: str,
        conversation_id: str | None = None,
        mode: str = "auto",
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
            metadata={"mode": mode},
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
        result = await self.llm.complete(llm_messages)
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

    def _prepare_context(self, message: str, conversation_id: str | None) -> AgentContext:
        if conversation_id is None:
            conversation_id = self.storage.create_conversation(self._title_from_goal(message))
        memory_hits = self.storage.search_memory(message[:120], limit=5)
        return AgentContext(conversation_id=conversation_id, memory_hits=memory_hits)

    def _build_llm_messages(self, context: AgentContext, message: str) -> list[dict[str, str]]:
        memory_block = ""
        if context.memory_hits:
            lines = [f"- {item['content']}" for item in context.memory_hits[:5]]
            memory_block = "Память, которая может быть полезна:\n" + "\n".join(lines)

        recent = self.storage.recent_messages(context.conversation_id, limit=12)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if memory_block:
            messages.append({"role": "system", "content": memory_block})
        for item in recent:
            if item["role"] in {"user", "assistant"}:
                messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": message})
        return messages

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
        return [
            "Зафиксировать цель, границы автономии и ожидаемый результат",
            "Собрать контекст: код, окружение, ограничения и доступные локальные ресурсы",
            "Разложить систему на runtime, память, инструменты, интерфейс и диагностику",
            "Реализовать минимальный рабочий вертикальный срез",
            "Подключить проверки, health-снимки и журнал решений",
            "Провести верификацию и оформить следующий исполнимый шаг",
        ]

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
