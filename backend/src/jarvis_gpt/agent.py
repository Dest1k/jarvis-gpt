from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

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

SYSTEM_PROMPT = """Ты JARVIS GPT: локальный агент Windows/WSL/Docker и личный операционный помощник.
Говори по-русски. Держи тон как у кинематографичного Jarvis: спокойный, точный, слегка ироничный,
с уважительной уверенностью и готовностью действовать, но без карикатурной театральности.
Работай как системный администратор Windows/Linux, web-исследователь, помощник по бытовым задачам
и аналитик по публичным источникам. Отделяй факты от предположений, фиксируй неопределенность.
Тяжелые локальные модели, кеши, данные и логи находятся вне репозитория в D:\\jarvis.
Если локальная LLM или инструмент недоступны, честно называй деградацию и предлагай следующий
проверяемый шаг, но не превращай это в отказ от всей задачи.

Capability contract:
- Не выдумывай policy refusal. Исторические, энциклопедические, журналистские, образовательные,
  исследовательские и OSINT-запросы разрешены, если оператор не просит причинить вред,
  украсть доступы, преследовать людей или обходить защиту.
- Если оператор просит открыть безопасный URL, Wikipedia/Google-поиск или локальную утилиту Windows,
  используй инструментальный маршрут Jarvis, а не отвечай, что у тебя нет браузера или GUI.
- Для Windows-задач используй native слой Jarvis: WMI/CIM для инвентаризации, WinAPI/окна/фокус,
  SendKeys/clipboard для GUI-ввода и PowerShell только как транспорт. Не ограничивайся консолью,
  если задача явно требует взаимодействия с окном или локальным приложением.
- Если оператор просит сделать действие "в консоли", "в браузере", "в калькуляторе", "в блокноте",
  "в окне" или в конкретном приложении, сначала открой/активируй эту среду и выполняй действие там.
  Не заменяй это текстовым примером команды, если доступен инструментальный маршрут.
- Если оператор просит посмотреть на экран его глазами, сделать скриншот, понять что видно в окне
  или проверить визуальное состояние, используй native screen capture и анализируй снимок/окна.
- Для системного администрирования предлагай PowerShell/Bash-команды, проверки, риски и rollback.
  Опасные или необратимые действия оформляй через approval/tool gate, а не отказывайся целиком.
- Для web/OSINT работай только с публичными источниками, структурируй найденное, сохраняй ссылки,
  помечай confidence и не выдавай предположения за факты.
- Не используй декоративные служебные префиксы и pseudo-tags вроде
  "$\\rightarrow$ **Важное уточнение:**".
  Пиши сразу человеческий ответ."""


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


@dataclass
class DirectAction:
    answer: str
    events: list[ChatEvent]


@dataclass
class NativeAction:
    action: str
    payload: dict[str, Any]
    answer: str


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

        direct_action = await self._try_direct_action(message, context)
        if direct_action is not None:
            for event in direct_action.events:
                events.append(event)
                await self._emit(event)
            message_id = self.storage.add_message(
                conversation_id=context.conversation_id,
                role="assistant",
                content=direct_action.answer,
                metadata={"events": [event.model_dump() for event in events]},
            )
            return ChatResponse(
                conversation_id=context.conversation_id,
                message_id=message_id,
                answer=direct_action.answer,
                events=events,
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
            answer = _clean_assistant_answer(result.content)
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

        direct_action = await self._try_direct_action(message)
        if direct_action is not None:
            for event in direct_action.events:
                events.append(event)
                await self._emit(event)
                yield {"type": "event", "event": event.model_dump()}
            yield {"type": "delta", "content": direct_action.answer}
            message_id = self.storage.add_message(
                conversation_id=context.conversation_id,
                role="assistant",
                content=direct_action.answer,
                metadata={"events": [event.model_dump() for event in events]},
            )
            yield {
                "type": "done",
                "answer": direct_action.answer,
                "conversation_id": context.conversation_id,
                "events": [event.model_dump() for event in events],
                "message_id": message_id,
            }
            return

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
            answer = _clean_assistant_answer("".join(answer_parts).strip())
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

    async def _try_direct_action(
        self,
        message: str,
        context: AgentContext | None = None,
    ) -> DirectAction | None:
        url = _browser_url_from_message(message)
        if url is not None:
            result = await self.tools.run("browser.open", {"url": url}, allow_danger=True)
            event = ChatEvent(
                type="tool_call",
                title="browser.open",
                content=result.summary,
                payload={"tool": result.tool, "ok": result.ok, "url": url},
            )
            verb = "Открыл" if result.ok else "Не смог открыть"
            return DirectAction(
                answer=f"{verb} вкладку: {url}\n\n{result.summary}",
                events=[event],
            )

        history_text = ""
        if context is not None:
            history_text = "\n".join(
                item["content"]
                for item in self.storage.recent_messages(context.conversation_id, limit=8)
                if item["role"] == "user"
            )
        native_action = _native_action_from_message(message, self.settings, history_text)
        if native_action is not None:
            result = await self.tools.run(
                "windows.native",
                {
                    "action": native_action.action,
                    "payload": native_action.payload,
                    "timeout_sec": 30,
                },
                allow_danger=True,
            )
            event = ChatEvent(
                type="tool_call",
                title=f"windows.native:{native_action.action}",
                content=result.summary,
                payload={
                    "tool": result.tool,
                    "ok": result.ok,
                    "action": native_action.action,
                },
            )
            status = "Готово" if result.ok else "Не смог выполнить native-действие"
            details = _native_result_excerpt(result)
            return DirectAction(
                answer=f"{status}: {native_action.answer}\n\n{result.summary}{details}",
                events=[event],
            )

        command = _host_command_from_message(message)
        if command is not None:
            result = await self.tools.run(
                "host.bridge.execute",
                {"command": command, "timeout_sec": 20},
                allow_danger=True,
            )
            event = ChatEvent(
                type="tool_call",
                title="host.bridge.execute",
                content=result.summary,
                payload={"tool": result.tool, "ok": result.ok},
            )
            verb = "Выполнил локальную команду" if result.ok else "Не смог выполнить команду"
            return DirectAction(
                answer=f"{verb}: `{command}`\n\n{result.summary}",
                events=[event],
            )

        return None

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


def _clean_assistant_answer(text: str) -> str:
    cleaned = re.sub(
        r"(?im)^\s*(?:\$\s*\\(?:rightarrow|to)\s*\$|\\(?:rightarrow|to)|→|->|⇒)?"
        r"\s*(?:\*\*)?(?:важное\s+уточнение|уточнение|important\s+note)\s*:?(?:\*\*)?\s*",
        "",
        text,
    )
    return cleaned.lstrip()


def _native_result_excerpt(result: ToolRunResponse) -> str:
    if not isinstance(result.data, dict):
        return ""
    native = result.data.get("native")
    if not isinstance(native, dict):
        return ""
    action = str(native.get("action") or result.data.get("action") or "")
    native_data = native.get("data")
    if not isinstance(native_data, dict):
        return ""
    if action == "wmi.query":
        return _format_native_rows(native_data.get("items"), title="Короткая выжимка:")
    if action == "window.list":
        return _format_native_rows(native_data.get("windows"), title="Видимые окна:")
    if action == "screen.capture":
        return _format_screen_capture(native_data)
    return ""


def _format_screen_capture(data: dict[str, Any]) -> str:
    lines = []
    path = data.get("path")
    width = data.get("width")
    height = data.get("height")
    if path:
        lines.append(f"- снимок: {path}")
    if width and height:
        lines.append(f"- размер: {width}x{height}")
    active = data.get("activeWindow")
    if isinstance(active, dict):
        title = active.get("MainWindowTitle") or active.get("mainWindowTitle") or ""
        process = active.get("ProcessName") or active.get("processName") or ""
        if title or process:
            lines.append(f"- активное окно: {_short_value(process)} — {_short_value(title)}")
    windows = _format_native_rows(data.get("windows"), title="Видимые окна:")
    if not lines and not windows:
        return ""
    return "\n\nВизуальная проверка:\n" + "\n".join(lines) + windows


def _format_native_rows(value: Any, *, title: str) -> str:
    if value is None:
        return ""
    rows = value if isinstance(value, list) else [value]
    rendered = []
    for item in rows[:5]:
        if isinstance(item, dict):
            fields = []
            for key, raw in item.items():
                if key.startswith("CIM") or key in {"PSComputerName", "Scope", "Path"}:
                    continue
                if raw is None:
                    continue
                fields.append(f"{key}={_short_value(raw)}")
            if fields:
                rendered.append("- " + "; ".join(fields[:4]))
        else:
            rendered.append(f"- {_short_value(item)}")
    if not rendered:
        return ""
    if len(rows) > len(rendered):
        rendered.append(f"- ... ещё {len(rows) - len(rendered)}")
    return "\n\n" + title + "\n" + "\n".join(rendered)


def _short_value(value: Any, max_chars: int = 100) -> str:
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


def _browser_url_from_message(message: str) -> str | None:
    normalized = message.lower()
    if not _contains_any(
        normalized,
        (
            "открой",
            "открыть",
            "open",
            "запусти",
            "новой вклад",
            "новую вклад",
            "гугл",
            "google",
            "загугли",
            "найди в интернете",
            "поиск",
        ),
    ):
        return None

    match = re.search(r"https?://[^\s)>\]]+", message)
    if match:
        return match.group(0).rstrip(".,;")

    search_query = _extract_web_search_query(message)
    if search_query:
        return f"https://www.google.com/search?q={quote(search_query)}"

    if not _contains_any(normalized, ("wiki", "вики", "wikipedia", "википед")):
        return None

    if _contains_any(normalized, ("рандом", "случайн", "random")):
        return "https://ru.wikipedia.org/wiki/Special:Random"
    if _contains_any(normalized, ("гитлер", "hitler")):
        return _wiki_article_url("Адольф Гитлер")

    topic = _extract_wiki_topic(message)
    if topic:
        return f"https://ru.wikipedia.org/w/index.php?search={quote(topic)}"
    return "https://ru.wikipedia.org/wiki/Заглавная_страница"


def _extract_web_search_query(message: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", message, flags=re.IGNORECASE)
    match = re.search(
        r"(?:загугли|погугли|google|найди\s+в\s+интернете|поиск(?:ай)?(?:\s+в\s+интернете)?)\s+(.+)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    query = re.sub(r"[.!?]+$", "", match.group(1)).strip(" ,:;")
    return query[:180]


def _extract_wiki_topic(message: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", message, flags=re.IGNORECASE)
    match = re.search(
        r"(?:стать[ьяю]\s+)?(?:про|о|about)\s+(.+?)(?:\s+на\s+(?:вики|wikipedia|википедии)|$)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    topic = re.sub(r"[.!?]+$", "", match.group(1)).strip(" ,:;")
    return topic[:120]


def _wiki_article_url(title: str) -> str:
    return "https://ru.wikipedia.org/wiki/" + title.replace(" ", "_")


APP_ALIASES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("калькулятор", "calculator", "calc.exe", "calc"), "calc.exe", "калькулятор"),
    (("блокнот", "notepad"), "notepad.exe", "блокнот"),
    (("paint", "mspaint", "паинт", "рисовал"), "mspaint.exe", "Paint"),
    (("проводник", "explorer"), "explorer.exe", "проводник"),
    (("chrome", "google chrome", "хром", "гугл хром"), "chrome.exe", "Chrome"),
    (("edge", "microsoft edge", "эдж"), "msedge.exe", "Microsoft Edge"),
    (("firefox", "фаерфокс", "файрфокс"), "firefox.exe", "Firefox"),
    (("word", "winword", "ворд"), "winword.exe", "Word"),
    (("excel", "эксель"), "excel.exe", "Excel"),
    (("powerpoint", "power point", "пауэрпоинт"), "powerpnt.exe", "PowerPoint"),
    (("vscode", "vs code", "visual studio code"), "Code.exe", "Visual Studio Code"),
    (("telegram", "телеграм"), "Telegram.exe", "Telegram"),
    (("диспетчер задач", "task manager", "taskmgr"), "taskmgr.exe", "диспетчер задач"),
    (("командную строку", "cmd", "консоль"), "cmd.exe", "командную строку"),
    (("powershell", "power shell", "пауэршелл"), "powershell.exe", "PowerShell"),
    (("terminal", "windows terminal", "терминал"), "wt.exe", "Windows Terminal"),
    (("службы", "services.msc"), "services.msc", "службы"),
    (("панель управления", "control panel"), "control.exe", "панель управления"),
    (
        ("диспетчер устройств", "device manager", "devmgmt.msc"),
        "devmgmt.msc",
        "диспетчер устройств",
    ),
)


def _native_action_from_message(
    message: str,
    settings: JarvisSettings | None = None,
    history_text: str = "",
) -> NativeAction | None:
    normalized = message.lower()
    screen_capture = _screen_capture_action(normalized, settings)
    if screen_capture is not None:
        return screen_capture

    largest_file_console = _largest_file_console_action(message, history_text)
    if largest_file_console is not None:
        return largest_file_console

    if _wants_top_process_console(normalized):
        return NativeAction(
            action="process.start",
            payload={
                "executable": "powershell.exe",
                "arguments": (
                    "-NoExit -Command "
                    '"Get-Process | Sort-Object CPU -Descending | '
                    "Select-Object -First 10 Name,Id,CPU,WorkingSet | "
                    'Format-Table -AutoSize"'
                ),
            },
            answer="открыл консоль с топ-10 процессов по CPU",
        )

    if _contains_any(normalized, ("wmi", "cim", "через wmi", "через cim")):
        return _wmi_action_from_message(message)

    if _contains_any(normalized, ("список окон", "покажи окна", "окна winapi", "list windows")):
        return NativeAction(
            action="window.list",
            payload={"limit": 30},
            answer="получил список видимых окон через WinAPI",
        )

    typed_text = _extract_text_to_type(message)
    if typed_text and not _app_from_message(normalized):
        return NativeAction(
            action="keyboard.send",
            payload={"text": typed_text},
            answer="ввёл текст в активное окно через native input",
        )

    app = _app_from_message(normalized)
    if app is None:
        return None
    _markers, executable, label = app
    wants_open = _contains_any(
        normalized,
        ("открой", "открыть", "запусти", "запустить", "open", "start", "посчитай"),
    )
    wants_typing = typed_text or _contains_any(
        normalized,
        ("набери", "введи", "напечат", "посчитай", "посчитать", "type", "write"),
    )
    if not wants_open and not wants_typing:
        return None

    if executable == "calc.exe" and wants_typing:
        keys = _calculator_keys_from_message(message)
        payload = {
            "executable": "explorer.exe",
            "arguments": r"shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
            "keys": keys,
            "wait_ms": 1800,
        }
        payload.update(_native_focus_hint(executable))
        return NativeAction(
            action="app.open_and_type",
            payload=payload,
            answer=f"открыл {label} и ввёл выражение",
        )

    if wants_typing and typed_text:
        payload = {
            "executable": executable,
            "text": typed_text,
            "wait_ms": 900,
        }
        payload.update(_native_focus_hint(executable))
        if executable == "notepad.exe" and settings is not None:
            scratch_path = _notepad_scratch_file(settings)
            payload["arguments"] = f'"{scratch_path}"'
            payload["window_title"] = Path(scratch_path).name
        return NativeAction(
            action="app.open_and_type",
            payload=payload,
            answer=f"открыл {label} и ввёл текст",
        )

    return NativeAction(
        action="process.start",
        payload={"executable": executable},
        answer=f"запустил {label}",
    )


def _wmi_action_from_message(message: str) -> NativeAction:
    normalized = message.lower()
    class_name = "Win32_OperatingSystem"
    properties = ["Caption", "Version", "BuildNumber", "LastBootUpTime"]
    label = "сведения об ОС"
    if _contains_any(normalized, ("процесс", "process")):
        class_name = "Win32_Process"
        properties = ["Name", "ProcessId", "CommandLine"]
        label = "процессы"
    elif _contains_any(normalized, ("служб", "service")):
        class_name = "Win32_Service"
        properties = ["Name", "State", "StartMode", "ProcessId"]
        label = "службы"
    elif _contains_any(normalized, ("gpu", "видеокарт", "video")):
        class_name = "Win32_VideoController"
        properties = ["Name", "AdapterRAM", "DriverVersion"]
        label = "видеоконтроллеры"
    elif _contains_any(normalized, ("bios", "биос")):
        class_name = "Win32_BIOS"
        properties = ["Manufacturer", "SMBIOSBIOSVersion", "ReleaseDate"]
        label = "BIOS"
    elif _contains_any(normalized, ("диск", "disk", "drive")):
        class_name = "Win32_LogicalDisk"
        properties = ["DeviceID", "DriveType", "Size", "FreeSpace"]
        label = "диски"

    explicit = re.search(r"\b(Win32_[A-Za-z0-9_]+)\b", message, flags=re.IGNORECASE)
    if explicit:
        class_name = explicit.group(1)
        properties = []
        label = class_name

    return NativeAction(
        action="wmi.query",
        payload={
            "namespace": "root\\cimv2",
            "class_name": class_name,
            "properties": properties,
            "limit": 20,
        },
        answer=f"получил {label} через WMI/CIM",
    )


def _screen_capture_action(
    normalized: str,
    settings: JarvisSettings | None,
) -> NativeAction | None:
    if settings is None:
        return None
    wants_screen = _contains_any(
        normalized,
        (
            "моими глазами",
            "твоими глазами",
            "посмотри экран",
            "на экран",
            "что на экране",
            "что видишь",
            "скриншот",
            "снимок экрана",
            "визуально",
            "в окне видно",
            "на картинке",
            "screenshot",
            "screen capture",
        ),
    )
    if not wants_screen:
        return None
    output_path = _screen_capture_file(settings)
    return NativeAction(
        action="screen.capture",
        payload={"path": str(output_path), "limit": 30},
        answer="сделал снимок экрана для визуальной проверки",
    )


def _screen_capture_file(settings: JarvisSettings) -> Path:
    screenshot_dir = settings.data_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    return screenshot_dir / f"screen-{uuid.uuid4().hex[:12]}.png"


def _largest_file_console_action(message: str, history_text: str = "") -> NativeAction | None:
    normalized = message.lower()
    history = history_text.lower()
    wants_console = _contains_any(
        normalized,
        (
            "в консоли",
            "консоль",
            "powershell",
            "power shell",
            "терминал",
            "командной строк",
            "вывод там же",
            "там же",
        ),
    )
    current_mentions_largest = _mentions_largest_file(normalized)
    followup_mentions_scan = _contains_any(
        normalized,
        ("это сканирование", "это проскан", "сканирование"),
    )
    history_mentions_largest = _mentions_largest_file(history)
    if not current_mentions_largest and not (
        wants_console and followup_mentions_scan and history_mentions_largest
    ):
        return None
    if not wants_console and not _contains_any(normalized, ("открой", "запусти", "сделай")):
        return None

    drive = _drive_from_largest_file_request(message, history_text)
    script = _largest_file_scan_script(drive)
    return NativeAction(
        action="process.start",
        payload={
            "executable": "powershell.exe",
            "arguments": _powershell_noexit_arguments(script),
        },
        answer=f"открыл PowerShell и запустил поиск самого крупного файла на диске {drive}",
    )


def _mentions_largest_file(text: str) -> bool:
    return _contains_any(
        text,
        (
            "самый крупный файл",
            "самого крупного файла",
            "самый большой файл",
            "самого большого файла",
            "largest file",
            "biggest file",
        ),
    )


def _drive_from_largest_file_request(message: str, history_text: str = "") -> str:
    match = re.search(r"\b([a-zA-Z]):", f"{message}\n{history_text}")
    if match:
        return f"{match.group(1).upper()}:\\"
    return "C:\\"


def _largest_file_scan_script(drive: str) -> str:
    quoted_drive = drive.replace("'", "''")
    return (
        "$ErrorActionPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"$root='{quoted_drive}'; "
        "Write-Host ('Сканирую ' + $root + ' ...') -ForegroundColor Cyan; "
        "$largest=$null; $count=0; "
        "Get-ChildItem -LiteralPath $root -File -Recurse -Force -ErrorAction SilentlyContinue | "
        "ForEach-Object { "
        "$count++; "
        "if ($null -eq $largest -or $_.Length -gt $largest.Length) { $largest=$_ }; "
        "if (($count % 10000) -eq 0) { "
        "Write-Host ('Проверено файлов: {0:n0}; текущий лидер: {1:n2} GB {2}' -f "
        "$count, ($largest.Length / 1GB), $largest.FullName) -ForegroundColor DarkGray "
        "} "
        "}; "
        "Write-Host ''; "
        "if ($largest) { "
        "Write-Host '--- Результат ---' -ForegroundColor Green; "
        "Write-Host ('Путь: ' + $largest.FullName) -ForegroundColor White; "
        "Write-Host ('Размер: {0:n2} GB ({1:n0} bytes)' -f "
        "($largest.Length / 1GB), $largest.Length) "
        "-ForegroundColor Yellow; "
        "Write-Host ('Проверено файлов: {0:n0}' -f $count) -ForegroundColor DarkGray "
        "} else { "
        "Write-Host 'Файлы не найдены или доступ ко всем каталогам запрещён.' -ForegroundColor Red "
        "}"
    )


def _powershell_noexit_arguments(script: str) -> str:
    escaped = script.replace('"', '`"')
    return f'-NoExit -ExecutionPolicy Bypass -Command "{escaped}"'


def _app_from_message(normalized: str) -> tuple[tuple[str, ...], str, str] | None:
    return next((item for item in APP_ALIASES if _contains_any(normalized, item[0])), None)


def _native_focus_hint(executable: str) -> dict[str, str]:
    hints = {
        "calc.exe": {
            "process_name": "CalculatorApp",
            "window_title": "Calculator|Калькулятор",
        },
        "notepad.exe": {
            "process_name": "notepad",
            "window_title": "Notepad|Блокнот",
        },
        "mspaint.exe": {
            "process_name": "mspaint",
            "window_title": "Paint",
        },
        "cmd.exe": {
            "process_name": "cmd",
            "window_title": "Command Prompt|Командная строка",
        },
        "powershell.exe": {
            "process_name": "powershell",
            "window_title": "Windows PowerShell|PowerShell",
        },
        "wt.exe": {
            "process_name": "WindowsTerminal",
            "window_title": "Windows PowerShell|PowerShell|Terminal|Терминал",
        },
    }
    return dict(hints.get(executable.lower(), {}))


def _notepad_scratch_file(settings: JarvisSettings) -> Path:
    scratch_dir = settings.data_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    path = scratch_dir / f"notepad-{uuid.uuid4().hex[:10]}.txt"
    path.write_text("", encoding="utf-8")
    return path


def _extract_text_to_type(message: str) -> str:
    match = re.search(
        r"(?:набери|введи|напечатай|напиши|type|write)\s+(.+)$",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    text = re.sub(r"\s+(?:в|внутри|в окне)\s+.+$", "", match.group(1), flags=re.IGNORECASE)
    return text.strip(" \"'«».,;")[:1000]


def _calculator_keys_from_message(message: str) -> str:
    compact = (
        message.replace("×", "*")
        .replace("÷", "/")
        .replace("х", "*")
        .replace("Х", "*")
        .replace("x", "*")
        .replace("X", "*")
    )
    match = re.search(r"(\d+(?:\s*[-+*/]\s*\d+)+)", compact)
    expression = match.group(1).replace(" ", "") if match else "123+456"
    return _sendkeys_for_calculator(f"{expression}=")


def _sendkeys_for_calculator(expression: str) -> str:
    replacements = {
        "+": "{+}",
        "-": "{-}",
        "*": "{*}",
        "/": "{/}",
    }
    return "".join(replacements.get(char, char) for char in expression)


def _host_command_from_message(message: str) -> str | None:
    normalized = message.lower()
    if not _contains_any(normalized, ("открой", "открыть", "запусти", "open", "start")):
        return None
    if not _contains_any(normalized, ("калькулятор", "calculator", "calc.exe", "calc")):
        return None
    command = "Start-Process calc.exe"
    if _contains_any(normalized, ("набери", "введи", "напечат", "type")):
        command += (
            "; Start-Sleep -Milliseconds 900"
            "; Add-Type -AssemblyName System.Windows.Forms"
            "; [System.Windows.Forms.SendKeys]::SendWait('123{+}456')"
        )
    return command


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _wants_top_process_console(normalized: str) -> bool:
    wants_console = _contains_any(
        normalized,
        ("консоль", "командную строку", "cmd", "powershell", "terminal", "терминал"),
    )
    wants_processes = _contains_any(normalized, ("процесс", "process"))
    wants_top = bool(re.search(r"\btop\s*10\b", normalized)) or _contains_any(
        normalized,
        ("топ 10", "топ-10", "top-10"),
    )
    wants_open = _contains_any(
        normalized,
        ("открой", "открыть", "запусти", "запустить", "open", "start"),
    )
    return wants_console and wants_processes and wants_top and wants_open


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
