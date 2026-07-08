from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
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
- Если запрос явно нацелен на консоль, не отвечай markdown-блоком с PowerShell.
  Используй console target guard: открой PowerShell/Terminal, выполни распознанный рецепт
  или команду там, а если команда неоднозначна, покажи диагностическое сообщение в самой консоли.
- Если оператор просит посмотреть на экран его глазами, сделать скриншот, понять что видно в окне
  или проверить визуальное состояние, используй native screen capture и анализируй снимок/окна.
- Для системного администрирования предлагай PowerShell/Bash-команды, проверки, риски и rollback.
  Опасные или необратимые действия оформляй через approval/tool gate, а не отказывайся целиком.
- Для web/OSINT работай только с публичными источниками, структурируй найденное, сохраняй ссылки,
  помечай confidence и не выдавай предположения за факты.
- Если запрос требует актуальной информации из интернета: билеты, цены, расписания, новости,
  наличие, курсы, погоду, адреса, телефоны, часы работы, открыто ли место сейчас,
  ближайшие бытовые точки или "послезавтра/сегодня/завтра", сначала используй web.search/web.fetch.
  Не пиши "запускаю поиск" и не имитируй результаты. Если поиск или сайт не отдал данные,
  прямо скажи, что именно не подтверждено, и дай проверяемые ссылки.
- Если вопрос ставит тебя в угол, зависит от сегодняшней реальности или есть риск ответить
  уверенной выдумкой, сначала честно гугли через web.search/web.fetch и анализируй найденное.
  Это относится не только к бытовым вопросам, но и к техническим, админским, разработческим,
  железным, финансовым, правовым и прочим меняющимся темам. Лучше показать источники
  и границы уверенности, чем красиво угадать.
- Всегда держи в уме текущую дату из runtime context. Если тема могла измениться после
  начала 2026 года или пользователь спрашивает про 2026+ / "сейчас" / свежую версию,
  не опирайся только на встроенные знания модели: сначала проверь источники.
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
    fallback: NativeAction | None = None


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

        direct_action = await self._try_direct_action(message, context)
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
        history_text = ""
        if context is not None:
            history_text = "\n".join(
                item["content"]
                for item in self.storage.recent_messages(context.conversation_id, limit=8)
                if item["role"] == "user"
            )
        active_console = None
        if context is not None:
            active_console = self._active_console_target(context.conversation_id)
        native_action = _native_action_from_message(
            message,
            self.settings,
            history_text,
            active_console,
        )
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
            events = [
                ChatEvent(
                    type="tool_call",
                    title=f"windows.native:{native_action.action}",
                    content=result.summary,
                    payload={
                        "tool": result.tool,
                        "ok": result.ok,
                        "action": native_action.action,
                    },
                )
            ]
            answer_action = native_action
            fallback_note = ""
            if not result.ok and native_action.fallback is not None:
                failed_summary = result.summary
                fallback = native_action.fallback
                result = await self.tools.run(
                    "windows.native",
                    {
                        "action": fallback.action,
                        "payload": fallback.payload,
                        "timeout_sec": 30,
                    },
                    allow_danger=True,
                )
                events.append(
                    ChatEvent(
                        type="tool_call",
                        title=f"windows.native:{fallback.action}",
                        content=result.summary,
                        payload={
                            "tool": result.tool,
                            "ok": result.ok,
                            "action": fallback.action,
                            "fallback_for": native_action.action,
                        },
                    )
                )
                answer_action = fallback
                fallback_note = f"\n\nПервичная попытка: {failed_summary}"
            if context is not None:
                self._remember_console_target(context.conversation_id, answer_action, result)
            status = "Готово" if result.ok else "Не смог выполнить native-действие"
            details = _native_result_excerpt(result)
            return DirectAction(
                answer=(
                    f"{status}: {answer_action.answer}\n\n"
                    f"{result.summary}{fallback_note}{details}"
                ),
                events=events,
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

        research_query = _web_research_query_from_message(message)
        if research_query is not None:
            return await self._run_web_research(message, research_query)

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

        return None

    async def _run_web_research(self, message: str, query: str) -> DirectAction:
        search = await self.tools.run("web.search", {"query": query, "limit": 6})
        events = [
            ChatEvent(
                type="tool_call",
                title="web.search",
                content=search.summary,
                payload={"tool": search.tool, "ok": search.ok, "query": query},
            )
        ]
        if not search.ok:
            return DirectAction(
                answer=(
                    "Не смог выполнить веб-поиск, поэтому не буду выдумывать результат.\n\n"
                    f"Запрос: `{query}`\nПричина: {search.summary}"
                ),
                events=events,
            )

        results = _search_results_from_response(search)
        if not results:
            for fallback_query in _fallback_web_research_queries(message, query):
                fallback = await self.tools.run("web.search", {"query": fallback_query, "limit": 6})
                events.append(
                    ChatEvent(
                        type="tool_call",
                        title="web.search",
                        content=fallback.summary,
                        payload={
                            "tool": fallback.tool,
                            "ok": fallback.ok,
                            "query": fallback_query,
                            "fallback": True,
                        },
                    )
                )
                if fallback.ok:
                    query = fallback_query
                    search = fallback
                    results = _search_results_from_response(search)
                    if results:
                        break
        fetches: list[ToolRunResponse] = []
        for item in results[:3]:
            fetched = await self.tools.run(
                "web.fetch",
                {"url": item["url"], "max_chars": 5000},
            )
            fetches.append(fetched)
            events.append(
                ChatEvent(
                    type="tool_call",
                    title="web.fetch",
                    content=fetched.summary,
                    payload={
                        "tool": fetched.tool,
                        "ok": fetched.ok,
                        "url": item["url"],
                    },
                )
            )
        return DirectAction(
            answer=_format_web_research_answer(
                message=message,
                query=query,
                results=results,
                fetches=fetches,
            ),
            events=events,
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
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": _runtime_date_context()},
        ]
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

    def _active_console_target(self, conversation_id: str) -> dict[str, Any] | None:
        value = self.storage.get_runtime_value(_console_target_key(conversation_id), None)
        return value if isinstance(value, dict) else None

    def _remember_console_target(
        self,
        conversation_id: str,
        action: NativeAction,
        result: ToolRunResponse,
    ) -> None:
        target = _console_target_from_result(action, result)
        if target is None:
            return
        self.storage.set_runtime_value(_console_target_key(conversation_id), target)

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


def _runtime_date_context() -> str:
    today = date.today()
    return "\n".join(
        [
            "Runtime date context:",
            f"- current_date: {today.isoformat()}",
            "- user_timezone: Europe/Moscow",
            "- practical_knowledge_horizon: treat model knowledge after early 2026 as uncertain.",
            "- if the answer depends on current versions, prices, schedules, laws, releases, "
            "hardware support, security status, news or anything after early 2026, "
            "use web tools first.",
        ]
    )


def _web_research_query_from_message(message: str) -> str | None:
    normalized = message.lower()
    explicit_open = _contains_any(
        normalized,
        ("открой", "открыть", "open", "новой вклад", "новую вклад", "в браузере"),
    )
    search_verbs = ("найди", "поищи", "узнай", "проверь")
    explicit_web_markers = (
        "гугл",
        "загугли",
        "погугли",
        "интернет",
        "в сети",
        "сайт",
        "источник",
        "ссылк",
    )
    live_data_markers = (
        "реальн",
        "актуаль",
        "сейчас",
        "сегодня",
        "завтра",
        "послезавтра",
        "цена",
        "стоимость",
        "билет",
        "рейс",
        "поезд",
        "расписание",
        "наличие",
        "новости",
        "курс",
        "котиров",
        "погода",
        "адрес",
        "телефон",
        "номер",
        "часы",
        "график",
        "режим работы",
        "открыт",
        "закрыт",
        "ближайш",
        "рядом",
        "поблизости",
        "как добраться",
        "где находится",
    )
    uncertainty_markers = (
        "актуально ли",
        "правда ли",
        "точно ли",
        "можно ли",
        "стоит ли",
        "что выбрать",
        "какой лучше",
        "какая лучше",
        "какое лучше",
        "лучший",
        "лучше",
        "сравни",
        "сравнение",
        "отзывы",
        "обзор",
        "рейтинг",
        "топ",
        "как сейчас",
        "не уверен",
        "не помню",
    )
    technical_freshness_markers = (
        "версия",
        "последняя версия",
        "latest",
        "release",
        "релиз",
        "changelog",
        "breaking change",
        "совместим",
        "compatibility",
        "поддерживает",
        "драйвер",
        "обновлен",
        "обновлён",
        "уязвим",
        "cve",
        "ошибка",
        "баг",
        "исправлен",
        "best practice",
        "рекомендации",
        "документация",
        "api",
        "sdk",
        "библиотек",
        "фреймворк",
        "docker image",
        "образ docker",
        "linux kernel",
        "windows server",
        "nvidia",
        "cuda",
        "vllm",
        "pytorch",
        "node",
        "python",
        "postgres",
        "nginx",
        "kubernetes",
    )
    osint_markers = (
        "человек",
        "люди",
        "персон",
        "фио",
        "номер",
        "телефон",
        "аккаунт",
        "ник",
        "username",
        "соцсет",
        "telegram",
        "телеграм",
        "email",
        "почт",
        "домен",
        "ip",
        "whois",
        "dns",
        "база",
        "бд",
        "утеч",
        "leak",
        "breach",
        "внешн",
        "публичн",
        "osint",
    )
    if explicit_open and not (
        _contains_any(normalized, search_verbs)
        or _contains_any(normalized, live_data_markers)
        or _mentions_post_knowledge_horizon(normalized)
        or _looks_like_place_lookup_query(normalized)
        or _looks_like_osint_query(normalized)
    ):
        return None
    if not (
        _contains_any(normalized, explicit_web_markers)
        or _contains_any(normalized, live_data_markers)
        or _contains_any(normalized, uncertainty_markers)
        or _mentions_post_knowledge_horizon(normalized)
        or _looks_like_technical_freshness_query(normalized, technical_freshness_markers)
        or _looks_like_shopping_query(normalized)
        or _looks_like_place_lookup_query(normalized)
        or (
            _contains_any(normalized, osint_markers)
            and not _looks_like_local_query(normalized)
        )
        or (
            _contains_any(normalized, search_verbs)
            and not _looks_like_local_query(normalized)
        )
    ):
        return None

    query = re.sub(r"https?://\S+", "", message, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip(" ,.;:")
    if not query:
        return None
    resolved_date = _relative_date_for_message(normalized)
    if resolved_date:
        query = f"{query} {resolved_date.isoformat()}"
    if _looks_like_shopping_query(normalized):
        query = _shopping_search_query(query, normalized)
    elif _looks_like_travel_query(normalized):
        query = f"{query} билеты цена наличие расписание официальный агрегатор"
    elif _looks_like_place_lookup_query(normalized):
        query = _place_lookup_search_query(query, normalized)
    elif _looks_like_technical_freshness_query(normalized, technical_freshness_markers):
        query = f"{query} official docs latest"
    elif _mentions_post_knowledge_horizon(normalized):
        query = f"{query} актуальные источники 2026"
    elif _contains_any(normalized, uncertainty_markers):
        query = f"{query} актуальные источники обзор сравнение"
    if _looks_like_osint_query(normalized) and not _looks_like_shopping_query(normalized):
        query = f"{query} публичные источники OSINT"
    return query[:300]


def _looks_like_local_query(normalized: str) -> bool:
    return _contains_any(
        normalized,
        (
            "лог",
            "docker",
            "докер",
            "контейнер",
            "процесс",
            "служб",
            "файл",
            "папк",
            "директор",
            "диск",
            "консол",
            "терминал",
            "powershell",
            "cmd",
            "windows",
            "wmi",
            "winapi",
            "gpu",
            "vram",
            "jarvis",
            "репозит",
            "проект",
        ),
    )


def _looks_like_technical_freshness_query(
    normalized: str,
    technical_freshness_markers: tuple[str, ...],
) -> bool:
    if _looks_like_local_runtime_query(normalized):
        return False
    return _contains_any(normalized, technical_freshness_markers)


def _looks_like_local_runtime_query(normalized: str) -> bool:
    if re.search(r"\bлог(?:и|ов|ами|ах)?\b", normalized):
        return True
    return _contains_any(
        normalized,
        (
            "контейнер",
            "процесс",
            "служб",
            "файл",
            "папк",
            "директор",
            "диск",
            "консол",
            "терминал",
            "powershell",
            "cmd",
            "wmi",
            "winapi",
            "gpu",
            "vram",
            "jarvis",
            "репозит",
            "проект",
            "у меня",
            "на моей",
            "на моём",
            "локальн",
        ),
    )


def _mentions_post_knowledge_horizon(normalized: str) -> bool:
    if _looks_like_local_runtime_query(normalized):
        return False
    if _contains_any(
        normalized,
        (
            "в этом году",
            "в текущем году",
            "на текущий момент",
            "по состоянию на",
            "после 2026",
            "с 2026",
            "с начала 2026",
            "новое сейчас",
            "новые сейчас",
            "свежие данные",
        ),
    ):
        return True
    years = [int(match) for match in re.findall(r"\b20\d{2}\b", normalized)]
    return any(year >= 2026 for year in years)


def _looks_like_osint_query(normalized: str) -> bool:
    if _looks_like_shopping_query(normalized):
        return False
    if _looks_like_place_lookup_query(normalized):
        return False
    if _contains_any(normalized, ("whois", "домен", "dns запись", "dns-зап", "dns record")):
        return True
    return _contains_any(
        normalized,
        (
            "человек",
            "люди",
            "фио",
            "номер",
            "телефон",
            "аккаунт",
            "ник",
            "username",
            "email",
            "почт",
            "домен",
            "whois",
            "утеч",
            "leak",
            "breach",
            "osint",
        ),
    )


def _looks_like_shopping_query(normalized: str) -> bool:
    purchase_context = _contains_any(
        normalized,
        (
            "купить",
            "дешев",
            "цена",
            "стоимость",
            "товар",
            "магазин",
            "продавец",
            "наличие",
            "заказ",
            "доставк",
            "скидк",
            "акци",
            "распродаж",
        ),
    )
    product_context = _contains_any(
        normalized,
        (
            "видеокарт",
            "ноутбук",
            "процессор",
            "ssd",
            "hdd",
            "rtx",
            "geforce",
            "radeon",
            "iphone",
            "смартфон",
            "телефон",
            "планшет",
            "монитор",
            "телевизор",
            "наушник",
            "клавиатур",
            "мышь",
        ),
    )
    store_context = _contains_any(
        normalized,
        (
            "dns",
            "днс",
            "ozon",
            "wildberries",
            "яндекс маркет",
            "yandex market",
            "маркет",
            "ситилинк",
            "citilink",
            "мвидео",
            "м.видео",
            "mvideo",
            "эльдорадо",
            "eldorado",
            "онлайнтрейд",
            "online trade",
            "avito",
            "авито",
            "aliexpress",
            "алиэкспресс",
        ),
    )
    if _looks_like_travel_query(normalized):
        return False
    if store_context and not _looks_like_osint_dns_context(normalized):
        return True
    return product_context and purchase_context


def _looks_like_osint_dns_context(normalized: str) -> bool:
    return _contains_any(normalized, ("whois", "домен", "dns запись", "dns-зап", "dns record"))


def _looks_like_place_lookup_query(normalized: str) -> bool:
    place_intent = _contains_any(
        normalized,
        (
            "адрес",
            "телефон",
            "номер",
            "часы",
            "график",
            "режим работы",
            "открыт",
            "закрыт",
            "ближайш",
            "рядом",
            "поблизости",
            "как добраться",
            "где находится",
        ),
    )
    place_subject = _contains_any(
        normalized,
        (
            "аптек",
            "магазин",
            "кафе",
            "ресторан",
            "банк",
            "банкомат",
            "мфц",
            "поликлиник",
            "больниц",
            "клиник",
            "почт",
            "пвз",
            "пункт выдачи",
            "школ",
            "садик",
            "сервис",
            "ремонт",
            "гибдд",
            "налогов",
            "паспортн",
            "метро",
            "остановк",
            "аэропорт",
            "вокзал",
            "отделен",
            "офис",
            "филиал",
        ),
    )
    if _looks_like_travel_query(normalized) and not place_intent:
        return False
    return place_intent and place_subject


def _place_lookup_search_query(query: str, normalized: str) -> str:
    subject = _clean_place_lookup_subject(query)
    suffix = "адрес телефон часы работы официальный сайт"
    if _contains_any(normalized, ("ближайш", "рядом", "поблизости", "как добраться")):
        suffix = f"{suffix} карта"
    if _contains_any(normalized, ("сегодня", "сейчас", "открыт", "закрыт")):
        suffix = f"{suffix} актуально сегодня"
    return f"{subject} {suffix}"


def _shopping_search_query(query: str, normalized: str) -> str:
    subject = _clean_shopping_subject(query)
    site_filter = _shopping_site_filter(normalized)
    if site_filter:
        subject = _compact_shopping_subject(subject)
    suffix = f"{site_filter} купить цена наличие" if site_filter else "купить цена наличие"
    return f"{subject} {suffix}"


def _fallback_web_research_queries(message: str, current_query: str) -> list[str]:
    normalized = message.lower()
    candidates: list[str] = []
    if _looks_like_shopping_query(normalized):
        subject = _compact_shopping_subject(_clean_shopping_subject(message))
        site_filter = _shopping_site_filter(normalized)
        domain_hint = _shopping_domain_hint(normalized)
        if domain_hint:
            candidates.append(f"{subject} {domain_hint} купить цена наличие")
            candidates.append(f"{subject} {domain_hint}")
        if site_filter:
            candidates.append(f"{subject} {site_filter}")
        if not candidates:
            candidates.append(f"{subject} цена наличие")
    elif _looks_like_place_lookup_query(normalized):
        subject = _clean_place_lookup_subject(message)
        candidates.append(f"{subject} адрес телефон часы работы")
    return _unique_search_queries(candidates, current_query)


def _shopping_site_filter(normalized: str) -> str:
    if _mentions_dns_store(normalized):
        return "site:dns-shop.ru"
    if _contains_any(normalized, ("ozon",)):
        return "site:ozon.ru"
    if _contains_any(normalized, ("wildberries",)):
        return "site:wildberries.ru"
    if _contains_any(normalized, ("яндекс маркет", "yandex market", "маркет")):
        return "site:market.yandex.ru"
    if _contains_any(normalized, ("ситилинк", "citilink")):
        return "site:citilink.ru"
    if _contains_any(normalized, ("мвидео", "м.видео", "mvideo")):
        return "site:mvideo.ru"
    if _contains_any(normalized, ("эльдорадо", "eldorado")):
        return "site:eldorado.ru"
    if _contains_any(normalized, ("avito", "авито")):
        return "site:avito.ru"
    return ""


def _shopping_domain_hint(normalized: str) -> str:
    site_filter = _shopping_site_filter(normalized)
    if site_filter.startswith("site:"):
        return site_filter.removeprefix("site:")
    return site_filter


def _unique_search_queries(candidates: list[str], current_query: str) -> list[str]:
    seen = {_normalize_search_query(current_query)}
    queries: list[str] = []
    for candidate in candidates:
        query = _normalize_search_query(candidate)
        if query and query not in seen:
            queries.append(query)
            seen.add(query)
    return queries


def _clean_shopping_subject(query: str) -> str:
    cleaned = _clean_research_subject(query)
    store_names = (
        "dns",
        "днс",
        "ozon",
        "wildberries",
        "яндекс маркет",
        "yandex market",
        "маркет",
        "ситилинк",
        "citilink",
        "мвидео",
        "м.видео",
        "mvideo",
        "эльдорадо",
        "eldorado",
        "авито",
        "avito",
        "aliexpress",
        "алиэкспресс",
    )
    store_pattern = "|".join(re.escape(name) for name in store_names)
    cleaned = re.sub(rf"\b(?:на|в|у)\s+(?:{store_pattern})\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bсам(?:ую|ый|ое|ые)\s+деш[её]в\w*\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bсам(?:ую|ый|ое|ые)\s+недорог\w*\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:купить|цена|стоимость|наличие)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = _normalize_search_query(cleaned)
    if cleaned:
        return cleaned
    return _normalize_search_query(query) or query


def _compact_shopping_subject(subject: str) -> str:
    tokens = re.findall(r"[\w.+-]+", subject, flags=re.IGNORECASE)
    technical: list[str] = []
    generic_prefixes = (
        "видеокарт",
        "ноутбук",
        "процессор",
        "смартфон",
        "телефон",
        "планшет",
        "монитор",
        "телевизор",
        "наушник",
        "клавиатур",
        "мыш",
        "товар",
    )
    for token in tokens:
        lower = token.lower()
        if any(lower.startswith(prefix) for prefix in generic_prefixes):
            continue
        if re.search(r"[a-z0-9]", lower, flags=re.IGNORECASE):
            technical.append(token)
    if technical and any(re.search(r"[a-z]", token, flags=re.IGNORECASE) for token in technical):
        return _normalize_search_query(" ".join(technical))
    return subject


def _clean_place_lookup_subject(query: str) -> str:
    cleaned = _clean_research_subject(query)
    cleaned = re.sub(
        r"\b(?:телефон|номер|часы работы|часы|график|режим работы)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:адрес|где находится|как добраться)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:и|а)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = _normalize_search_query(cleaned)
    return cleaned or _normalize_search_query(query) or query


def _clean_research_subject(query: str) -> str:
    cleaned = query
    command_patterns = (
        r"^\s*дай\s+мне\s+(?:пример\s+)?(?:реальн\w+\s+)?",
        r"^\s*(?:найди|поищи|узнай|проверь|покажи|подскажи|подбери)\s+(?:мне\s+)?",
        r"^\s*(?:найти|поискать|проверить|узнать|показать|подобрать)\s+",
    )
    for pattern in command_patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:пожалуйста|плиз|мне)\b", " ", cleaned, flags=re.IGNORECASE)
    return _normalize_search_query(cleaned)


def _normalize_search_query(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip(" ,.;:")


def _mentions_dns_store(normalized: str) -> bool:
    return bool(re.search(r"(?<![a-zа-яё0-9])(?:dns|днс)(?![a-zа-яё0-9])", normalized))


def _relative_date_for_message(normalized: str) -> date | None:
    today = date.today()
    if "послезавтра" in normalized:
        return today + timedelta(days=2)
    if "завтра" in normalized:
        return today + timedelta(days=1)
    if "сегодня" in normalized:
        return today
    return None


def _looks_like_travel_query(normalized: str) -> bool:
    return _contains_any(
        normalized,
        (
            "билет",
            "рейс",
            "авиа",
            "самолет",
            "самолёт",
            "поезд",
            "ржд",
            "аэропорт",
            "вылет",
            "прилет",
            "прилёт",
            "маршрут",
        ),
    )


def _format_web_research_answer(
    *,
    message: str,
    query: str,
    results: list[dict[str, Any]],
    fetches: list[ToolRunResponse],
) -> str:
    normalized = message.lower()
    date_note = _relative_date_for_message(normalized)
    shopping = _looks_like_shopping_query(normalized)
    place_lookup = _looks_like_place_lookup_query(normalized)
    travel = _looks_like_travel_query(normalized)
    osint = _looks_like_osint_query(normalized)
    lines = ["Проверил веб-поиск."]
    if date_note:
        lines.append(f"Дата из запроса: {date_note.isoformat()}.")
    lines.append(f"Поисковый запрос: `{query}`.")
    if not results:
        lines.append(
            _no_results_research_message(
                shopping=shopping,
                place_lookup=place_lookup,
                travel=travel,
                osint=osint,
            )
        )
        return "\n".join(lines)

    evidence = _research_evidence(results, fetches)
    if shopping:
        facts = _extract_shopping_facts(evidence)
        if _mentions_dns_store(normalized):
            lines.append("\nПриоритетно проверял выдачу магазина DNS (`dns-shop.ru`).")
        if facts["prices"] or facts["availability"]:
            lines.append("\nЧто удалось вытащить из найденных страниц/сниппетов:")
            if facts["prices"]:
                lines.append(f"- цены/предложения: {', '.join(facts['prices'][:6])}")
            if facts["availability"]:
                lines.append(f"- наличие/доставка: {', '.join(facts['availability'][:6])}")
            lines.append(
                "- это не заказ и не гарантия склада: финальную цену, город и наличие "
                "нужно подтвердить на карточке продавца."
            )
        else:
            lines.append(
                "\nПоиск нашёл источники по товару, но статические страницы "
                "не отдали точную цену или наличие. Не выдумываю."
            )
    elif place_lookup:
        facts = _extract_place_lookup_facts(evidence)
        if facts["phones"] or facts["hours"] or facts["addresses"]:
            lines.append("\nЧто удалось вытащить из найденных страниц/сниппетов:")
            if facts["phones"]:
                lines.append(f"- телефоны: {', '.join(facts['phones'][:4])}")
            if facts["hours"]:
                lines.append(f"- время/режим: {', '.join(facts['hours'][:6])}")
            if facts["addresses"]:
                lines.append(f"- адресные фрагменты: {', '.join(facts['addresses'][:4])}")
            lines.append(
                "- это не гарантия актуального режима: часы работы и доступность "
                "нужно подтвердить на странице организации или карте."
            )
        else:
            lines.append(
                "\nПоиск нашёл источники по месту/организации, но статические страницы "
                "не отдали телефон, адрес или график. Не выдумываю."
            )
    elif travel:
        facts = _extract_travel_facts(evidence)
        if facts["prices"] or facts["times"]:
            lines.append("\nЧто удалось вытащить из найденных страниц/сниппетов:")
            if facts["prices"]:
                lines.append(f"- цены/тарифы: {', '.join(facts['prices'][:5])}")
            if facts["times"]:
                lines.append(f"- время в материалах: {', '.join(facts['times'][:8])}")
            lines.append(
                "- это не бронь и не гарантия наличия: финальную карточку билета "
                "нужно подтверждать на сайте продавца."
            )
        else:
            lines.append(
                "\nПоиск нашёл источники по маршруту, но статические страницы "
                "не отдали точную карточку билета с ценой/временем. Не выдумываю."
            )

    lines.append("\nИсточники:")
    for index, item in enumerate(evidence[:6], start=1):
        snippet = f" — {item['snippet']}" if item.get("snippet") else ""
        lines.append(f"{index}. {item['title']}: {item['url']}{snippet}")
    if travel:
        lines.append(
            "\nПрактичный следующий шаг: открыть 1-2 источника из списка "
            "и выбрать конкретный рейс/поезд в живой выдаче."
        )
    if shopping:
        lines.append(
            "\nПрактичный следующий шаг: открыть 1-2 карточки из списка и отсортировать "
            "их по цене уже в живой выдаче магазина."
        )
    if place_lookup:
        lines.append(
            "\nПрактичный следующий шаг: открыть официальный сайт или карточку на карте "
            "и проверить режим работы для нужного города/района."
        )
    if osint:
        lines.append(
            "\nOSINT-рамка: использовал только публичные источники. "
            "Я могу структурировать найденное, "
            "но не буду помогать со взломом, обходом доступа, доксом или преследованием людей."
        )
    return "\n".join(lines)


def _no_results_research_message(
    *,
    shopping: bool,
    place_lookup: bool,
    travel: bool,
    osint: bool,
) -> str:
    if shopping:
        return (
            "\nНичего подтверждённого по товару не нашёл. "
            "Придумывать цену, магазин или наличие не буду."
        )
    if place_lookup:
        return (
            "\nНичего подтверждённого по месту/организации не нашёл. "
            "Придумывать адрес, телефон или часы работы не буду."
        )
    if travel:
        return (
            "\nНичего подтверждённого по маршруту не нашёл. "
            "Придумывать билет, цену или расписание не буду."
        )
    if osint:
        return (
            "\nНичего подтверждённого в публичных источниках не нашёл. "
            "Придумывать совпадения, аккаунты или утечки не буду."
        )
    return "\nНичего подтверждённого не нашёл. Придумывать факты не буду."


def _search_results_from_response(search: ToolRunResponse) -> list[dict[str, Any]]:
    return [
        item
        for item in search.data.get("results", [])
        if isinstance(item, dict) and item.get("url")
    ][:6]


def _research_evidence(
    results: list[dict[str, Any]],
    fetches: list[ToolRunResponse],
) -> list[dict[str, str]]:
    fetched_by_url = {
        str(item.data.get("url") or ""): str(item.data.get("text") or "")
        for item in fetches
        if item.ok and isinstance(item.data, dict)
    }
    evidence: list[dict[str, str]] = []
    for result in results:
        url = str(result.get("url") or "")
        fetched_text = fetched_by_url.get(url, "")
        snippet = str(result.get("snippet") or "")
        if fetched_text:
            snippet = _short_value(fetched_text, 240)
        evidence.append(
            {
                "title": str(result.get("title") or url),
                "url": url,
                "snippet": snippet,
            }
        )
    return evidence


def _extract_travel_facts(evidence: list[dict[str, str]]) -> dict[str, list[str]]:
    text = " ".join(item.get("snippet", "") for item in evidence)
    prices = _dedupe(
        [
            " ".join(match.split())
            for match in re.findall(
                r"(?:от\s*)?\d[\d\s]{2,}\s*(?:₽|руб\.?|rub)",
                text,
                flags=re.IGNORECASE,
            )
        ]
    )
    times = _dedupe(re.findall(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b", text))
    return {"prices": prices, "times": times}


def _extract_shopping_facts(evidence: list[dict[str, str]]) -> dict[str, list[str]]:
    text = " ".join(item.get("snippet", "") for item in evidence)
    availability_pattern = (
        r"(?:в наличии|нет в наличии|под заказ|доступно к заказу|самовывоз|"
        r"доставка[^,.]{0,40})"
    )
    prices = _dedupe(
        [
            " ".join(match.split())
            for match in re.findall(
                r"(?:от\s*)?\d[\d\s]{2,}\s*(?:₽|руб\.?|rub)",
                text,
                flags=re.IGNORECASE,
            )
        ]
    )
    availability = _dedupe(
        [
            " ".join(match.split())
            for match in re.findall(
                availability_pattern,
                text,
                flags=re.IGNORECASE,
            )
        ]
    )
    return {"prices": prices, "availability": availability}


def _extract_place_lookup_facts(evidence: list[dict[str, str]]) -> dict[str, list[str]]:
    text = " ".join(item.get("snippet", "") for item in evidence)
    phones = _dedupe(
        [
            " ".join(match.split())
            for match in re.findall(
                r"(?:\+7|8)\s*[\-(]?\d{3}[\-) ]*\d{3}[- ]?\d{2}[- ]?\d{2}",
                text,
            )
        ]
    )
    hours = _dedupe(
        [
            " ".join(match.split())
            for match in re.findall(
                r"(?:круглосуточно|24/7|ежедневно|сегодня[^,.]{0,40}|"
                r"(?:[01]?\d|2[0-3])[:.][0-5]\d\s*[-–]\s*"
                r"(?:[01]?\d|2[0-3])[:.][0-5]\d)",
                text,
                flags=re.IGNORECASE,
            )
        ]
    )
    addresses = _dedupe(
        [
            " ".join(match.split())
            for match in re.findall(
                r"(?:ул\.?|улица|проспект|пр-т|шоссе|площадь|пер\.?)\s+[^,.]{3,80}",
                text,
                flags=re.IGNORECASE,
            )
        ]
    )
    return {"phones": phones, "hours": hours, "addresses": addresses}


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
    (("командную строку", "командной строк", "cmd", "консол"), "cmd.exe", "командную строку"),
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
    active_console: dict[str, Any] | None = None,
) -> NativeAction | None:
    normalized = message.lower()
    screen_capture = _screen_capture_action(normalized, settings)
    if screen_capture is not None:
        return screen_capture

    same_console = _same_console_action(message, history_text, active_console)
    if same_console is not None:
        return same_console

    system_info_console = _system_info_console_action(message, history_text)
    if system_info_console is not None:
        return system_info_console

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

    console_guard = _console_target_guard_action(message, history_text)
    if console_guard is not None:
        return console_guard

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


def _system_info_console_action(message: str, history_text: str = "") -> NativeAction | None:
    normalized = message.lower()
    history = history_text.lower()
    wants_console = _wants_console_target(normalized)
    current_mentions_system = _mentions_system_info(normalized)
    followup_mentions_console = wants_console and _contains_any(
        normalized,
        ("именно", "туда", "там", "открой", "запусти", "сделай", "вывод"),
    )
    if not current_mentions_system and not (
        followup_mentions_console and _mentions_system_info(history)
    ):
        return None
    if not wants_console and not _contains_any(normalized, ("открой", "запусти", "сделай")):
        return None

    return NativeAction(
        action="process.start",
        payload={
            "executable": "powershell.exe",
            "arguments": _powershell_noexit_arguments(_system_info_script()),
        },
        answer="открыл PowerShell и вывел информацию о системе",
    )


def _mentions_system_info(text: str) -> bool:
    if _contains_any(text, ("system information", "systeminfo", "computerinfo")):
        return True
    if _contains_any(text, ("о системе", "про систему")):
        return True
    return _contains_any(text, ("систем",)) and _contains_any(
        text,
        ("информац", "инфу", "сведен", "сводк", "характерист", "спецификац", "конфигурац"),
    )


def _system_info_script() -> str:
    return (
        "$ErrorActionPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Write-Host '--- SYSTEM INFORMATION ---' -ForegroundColor Cyan; "
        "Get-ComputerInfo | Select-Object OsName,OsVersion,OsArchitecture,CsName,"
        "CsManufacturer,CsModel,CsProcessors,CsTotalPhysicalMemory | Format-List; "
        "Write-Host '--- CPU ---' -ForegroundColor Cyan; "
        "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
        "NumberOfLogicalProcessors,MaxClockSpeed | Format-List; "
        "Write-Host '--- MEMORY ---' -ForegroundColor Cyan; "
        "Get-CimInstance Win32_PhysicalMemory | Measure-Object Capacity -Sum | "
        "ForEach-Object { Write-Host ('Total RAM: {0:n2} GB' -f ($_.Sum / 1GB)) }; "
        "Write-Host '--- DISKS ---' -ForegroundColor Cyan; "
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
        "Select-Object DeviceID,@{Name='SizeGB';Expression={[math]::Round($_.Size/1GB,2)}},"
        "@{Name='FreeGB';Expression={[math]::Round($_.FreeSpace/1GB,2)}} | "
        "Format-Table -AutoSize; "
        "Write-Host '--- GPU ---' -ForegroundColor Cyan; "
        "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion | "
        "Format-Table -AutoSize"
    )


def _largest_file_console_action(message: str, history_text: str = "") -> NativeAction | None:
    normalized = message.lower()
    history = history_text.lower()
    wants_console = _wants_console_target(normalized)
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


def _same_console_action(
    message: str,
    history_text: str,
    active_console: dict[str, Any] | None,
) -> NativeAction | None:
    if not active_console:
        return None
    normalized = message.lower()
    if not _wants_existing_console_target(normalized):
        return None

    shell = _console_shell_from_target(active_console)
    command = _existing_console_command(message, history_text, shell)
    payload = _console_keyboard_payload(active_console, command)
    fallback = _console_process_fallback(command, shell)
    return NativeAction(
        action="keyboard.send",
        payload=payload,
        answer="отправил команду в уже открытую консоль",
        fallback=fallback,
    )


def _wants_existing_console_target(normalized: str) -> bool:
    same_target = _contains_any(
        normalized,
        (
            "этой же",
            "той же",
            "эту же",
            "ту же",
            "в этой",
            "в той",
            "там же",
            "туда же",
            "сюда же",
            "в ней",
            "в него",
            "текущ",
            "уже открыт",
            "same console",
            "same terminal",
        ),
    )
    console_word = _contains_any(
        normalized,
        (
            "консол",
            "косол",
            "терминал",
            "terminal",
            "powershell",
            "cmd",
            "командн",
        ),
    )
    if same_target and (console_word or _contains_any(normalized, ("там же", "туда же"))):
        return True
    return _contains_any(normalized, ("теперь", "сейчас", "следом")) and _wants_console_target(
        normalized
    )


def _existing_console_command(message: str, history_text: str, shell: str) -> str:
    explicit = _extract_explicit_console_command(message)
    if explicit:
        return explicit

    normalized = message.lower()
    combined = f"{normalized}\n{history_text.lower()}"
    if _mentions_system_info(combined):
        return _system_info_existing_console_command(shell)
    if _mentions_network_info(combined):
        return _network_existing_console_command(shell)
    if _mentions_largest_file(combined):
        drive = _drive_from_largest_file_request(message, history_text)
        return _shell_command_for_existing_console(_largest_file_scan_script(drive), shell)
    if _mentions_top_processes(combined):
        script = (
            "Get-Process | Sort-Object CPU -Descending | "
            "Select-Object -First 10 Name,Id,CPU,WorkingSet | Format-Table -AutoSize"
        )
        return _shell_command_for_existing_console(script, shell)
    return _same_console_guard_command(message, shell)


def _system_info_existing_console_command(shell: str) -> str:
    if shell == "cmd":
        return "systeminfo"
    return _system_info_script()


def _network_existing_console_command(shell: str) -> str:
    if shell == "cmd":
        return "ipconfig /all"
    return _network_info_script()


def _shell_command_for_existing_console(script: str, shell: str) -> str:
    if shell == "cmd":
        return _powershell_invocation_for_cmd(script)
    return script


def _powershell_invocation_for_cmd(script: str) -> str:
    escaped = script.replace('"', '`"')
    return f'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "{escaped}"'


def _same_console_guard_command(message: str, shell: str) -> str:
    text = (
        "Jarvis: запрос нацелен на эту же консоль, "
        "но команда или готовый рецепт не распознаны."
    )
    if shell == "cmd":
        return f"echo {text}"
    return f"Write-Host {_ps_single_quoted(text)} -ForegroundColor Yellow"


def _console_keyboard_payload(target: dict[str, Any], command: str) -> dict[str, Any]:
    return {
        "process_id": _int_or_zero(target.get("pid")),
        "process_name": str(target.get("process_name") or ""),
        "window_title": str(target.get("window_title") or ""),
        "text": command,
        "keys": "{ENTER}",
    }


def _console_process_fallback(command: str, shell: str) -> NativeAction:
    if shell == "cmd":
        return NativeAction(
            action="process.start",
            payload={"executable": "cmd.exe", "arguments": f"/k {command}"},
            answer="не смог сфокусировать прежнюю консоль, открыл новую cmd и выполнил команду",
        )
    return NativeAction(
        action="process.start",
        payload={
            "executable": "powershell.exe",
            "arguments": _powershell_noexit_arguments(command),
        },
        answer=(
            "не смог сфокусировать прежнюю консоль, "
            "открыл новый PowerShell и выполнил команду"
        ),
    )


def _console_target_key(conversation_id: str) -> str:
    return f"ui.target.console.{conversation_id}"


def _console_target_from_result(
    action: NativeAction,
    result: ToolRunResponse,
) -> dict[str, Any] | None:
    if not result.ok:
        return None
    payload = action.payload
    if action.action == "keyboard.send" and _payload_targets_console(payload):
        shell = _console_shell_from_target(payload)
        return {
            "pid": _int_or_zero(payload.get("process_id")),
            "process_name": str(payload.get("process_name") or ""),
            "window_title": str(payload.get("window_title") or ""),
            "executable": "",
            "shell": shell,
        }
    if action.action not in {"process.start", "app.open_and_type"}:
        return None

    executable = str(payload.get("executable") or "")
    if not _is_console_executable(executable):
        return None
    native_data = _native_result_data(result)
    process_name = str(native_data.get("processName") or native_data.get("ProcessName") or "")
    hints = _native_focus_hint(executable)
    if not process_name:
        process_name = str(hints.get("process_name") or Path(executable).stem)
    shell = _console_shell_from_executable(executable, process_name)
    return {
        "pid": _int_or_zero(native_data.get("pid") or native_data.get("Id")),
        "process_name": str(hints.get("process_name") or process_name),
        "window_title": str(hints.get("window_title") or ""),
        "executable": executable,
        "shell": shell,
    }


def _native_result_data(result: ToolRunResponse) -> dict[str, Any]:
    if not isinstance(result.data, dict):
        return {}
    native = result.data.get("native")
    if not isinstance(native, dict):
        return {}
    data = native.get("data")
    return data if isinstance(data, dict) else {}


def _payload_targets_console(payload: dict[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(key) or "").lower()
        for key in ("process_name", "window_title", "executable")
    )
    return _contains_any(
        text,
        ("cmd", "powershell", "pwsh", "windowsterminal", "terminal", "команд", "терминал"),
    )


def _is_console_executable(executable: str) -> bool:
    name = Path(executable).name.lower()
    return name in {"cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe"}


def _console_shell_from_target(target: dict[str, Any]) -> str:
    shell = str(target.get("shell") or "").lower()
    if shell in {"cmd", "powershell", "terminal"}:
        return "powershell" if shell == "terminal" else shell
    executable = str(target.get("executable") or "")
    process_name = str(target.get("process_name") or "")
    return _console_shell_from_executable(executable, process_name)


def _console_shell_from_executable(executable: str, process_name: str = "") -> str:
    text = f"{Path(executable).name} {process_name}".lower()
    if "cmd" in text:
        return "cmd"
    return "powershell"


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mentions_top_processes(text: str) -> bool:
    wants_processes = _contains_any(text, ("процесс", "process"))
    wants_top = bool(re.search(r"\btop\s*10\b", text)) or _contains_any(
        text,
        ("топ 10", "топ-10", "top-10"),
    )
    return wants_processes and wants_top


def _console_target_guard_action(message: str, history_text: str = "") -> NativeAction | None:
    normalized = message.lower()
    if not _wants_console_target(normalized):
        return None
    if _is_plain_console_open_request(normalized):
        return None

    explicit_command = _extract_explicit_console_command(message)
    if explicit_command:
        script = _console_command_script(explicit_command)
        return NativeAction(
            action="process.start",
            payload={
                "executable": "powershell.exe",
                "arguments": _powershell_noexit_arguments(script),
            },
            answer="открыл PowerShell и выполнил распознанную команду в консоли",
        )

    combined = f"{normalized}\n{history_text.lower()}"
    if _mentions_network_info(combined):
        script = _network_info_script()
        return NativeAction(
            action="process.start",
            payload={
                "executable": "powershell.exe",
                "arguments": _powershell_noexit_arguments(script),
            },
            answer="открыл PowerShell и вывел сетевую диагностику в консоли",
        )

    script = _console_guard_fallback_script(message)
    return NativeAction(
        action="process.start",
        payload={
            "executable": "powershell.exe",
            "arguments": _powershell_noexit_arguments(script),
        },
        answer="открыл PowerShell с диагностикой console target guard",
    )


def _is_plain_console_open_request(normalized: str) -> bool:
    wants_open = _contains_any(
        normalized,
        ("открой", "открыть", "запусти", "запустить", "open", "start"),
    )
    if not wants_open:
        return False
    task_markers = (
        "выполни",
        "сделай",
        "покажи",
        "выведи",
        "найди",
        "провер",
        "диагност",
        "информац",
        "сведен",
        "скан",
        "топ",
        "top",
        "процесс",
        "сет",
        "ip",
        "dns",
        "wmi",
        "cim",
        "список",
        "настрой",
    )
    return not _contains_any(normalized, task_markers)


def _extract_explicit_console_command(message: str) -> str:
    fenced = re.search(r"```(?:[a-zA-Z0-9_-]+)?\s*(.*?)```", message, flags=re.DOTALL)
    if fenced:
        command = _compact_shell_command(fenced.group(1))
        if _looks_like_shell_command(command):
            return command

    for quoted in re.finditer(r"`([^`\r\n]{2,1800})`", message):
        command = _compact_shell_command(quoted.group(1))
        if _looks_like_shell_command(command):
            return command

    markers = r"(?:консол\w*|powershell|power shell|пауэршелл|терминал\w*|terminal|cmd)"
    patterns = (
        rf"(?:выполни|запусти|введи|набери)\s+(?:в\s+)?{markers}\s+(.+)$",
        rf"(?:выполни|запусти|введи|набери)\s+(.+?)\s+(?:в\s+)?{markers}\s*$",
        rf"(?:в\s+)?{markers}\s*[:\-]\s*(.+)$",
        rf"(?:в\s+)?{markers}\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if not match:
            continue
        command = _compact_shell_command(match.group(1).strip(" \"'«».,;"))
        if _looks_like_shell_command(command):
            return command
    return ""


def _compact_shell_command(command: str) -> str:
    lines = [line.strip() for line in command.replace("\r", "\n").split("\n") if line.strip()]
    return "; ".join(lines)[:1800]


def _looks_like_shell_command(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    first = re.split(r"\s+", lowered, maxsplit=1)[0].strip("&.")
    prefixes = (
        "$",
        "cd",
        "choco",
        "cmd",
        "curl",
        "dir",
        "dism",
        "docker",
        "git",
        "get-",
        "gwmi",
        "hostname",
        "ipconfig",
        "invoke-",
        "ls",
        "net",
        "netstat",
        "new-",
        "node",
        "npm",
        "nslookup",
        "ping",
        "pnpm",
        "powershell",
        "pwsh",
        "py",
        "python",
        "reg",
        "remove-",
        "resolve-",
        "restart-",
        "route",
        "sc",
        "set-",
        "sfc",
        "start-",
        "stop-",
        "systeminfo",
        "taskkill",
        "tasklist",
        "test-",
        "tracert",
        "where",
        "where-object",
        "whoami",
        "winget",
        "write-",
        "wsl",
    )
    if first.endswith((".exe", ".bat", ".cmd", ".ps1")):
        return True
    has_command_prefix = any(
        first.startswith(prefix) for prefix in prefixes if prefix.endswith("-")
    )
    if first in prefixes or has_command_prefix:
        return True
    has_cyrillic = bool(re.search(r"[а-яё]", lowered))
    if re.search(r"[|;&<>]", stripped) and not has_cyrillic:
        return True
    return bool(re.search(r"\s[-/][A-Za-z?]", stripped) and not has_cyrillic)


def _mentions_network_info(text: str) -> bool:
    has_network_word = _contains_any(
        text,
        (
            "network",
            "netadapter",
            "netipconfiguration",
            "ipconfig",
            "dns",
            "сет",
            "интернет",
            "адаптер",
            "ip адрес",
            "айпи",
            "шлюз",
            "маршрут",
        ),
    )
    if not has_network_word:
        return False
    return _contains_any(
        text,
        (
            "диагност",
            "информац",
            "настрой",
            "покажи",
            "выведи",
            "проверь",
            "сведен",
            "ipconfig",
            "dns",
            "сет",
            "интернет",
        ),
    )


def _console_command_script(command: str) -> str:
    quoted = _ps_single_quoted(command)
    return (
        "$ErrorActionPreference='Continue'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Write-Host '--- JARVIS CONSOLE TARGET ---' -ForegroundColor Cyan; "
        f"Write-Host ('Command: ' + {quoted}) -ForegroundColor DarkGray; "
        f"{command}"
    )


def _network_info_script() -> str:
    return (
        "$ErrorActionPreference='Continue'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Write-Host '--- NETWORK DIAGNOSTICS ---' -ForegroundColor Cyan; "
        "Write-Host '--- IPCONFIG ---' -ForegroundColor DarkCyan; "
        "ipconfig /all; "
        "Write-Host '--- ADAPTERS ---' -ForegroundColor DarkCyan; "
        "Get-NetAdapter | Select-Object Name,Status,LinkSpeed,MacAddress | Format-Table -AutoSize; "
        "Write-Host '--- IP CONFIGURATION ---' -ForegroundColor DarkCyan; "
        "Get-NetIPConfiguration | Format-List; "
        "Write-Host '--- DNS CLIENT ---' -ForegroundColor DarkCyan; "
        "Get-DnsClientServerAddress | Format-Table -AutoSize"
    )


def _console_guard_fallback_script(message: str) -> str:
    request = _ps_single_quoted(message.strip()[:600])
    return (
        "$ErrorActionPreference='Continue'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Write-Host '--- JARVIS CONSOLE TARGET GUARD ---' -ForegroundColor Yellow; "
        "Write-Host 'Запрос явно нацелен на консоль, поэтому я не отвечаю "
        "примером команды в чате.'; "
        f"Write-Host ('Запрос: ' + {request}) -ForegroundColor DarkGray; "
        "Write-Host 'Команда или готовый рецепт не распознаны однозначно.' "
        "-ForegroundColor Yellow; "
        "Write-Host 'Сформулируйте конкретную команду в обратных кавычках "
        "или назовите тип диагностики.'"
    )


def _ps_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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


def _wants_console_target(normalized: str) -> bool:
    return _contains_any(
        normalized,
        (
            "в консоли",
            "консол",
            "powershell",
            "power shell",
            "пауэршелл",
            "терминал",
            "terminal",
            "командн",
            "cmd",
            "вывод там же",
            "там же",
        ),
    )


def _wants_top_process_console(normalized: str) -> bool:
    wants_console = _wants_console_target(normalized)
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
