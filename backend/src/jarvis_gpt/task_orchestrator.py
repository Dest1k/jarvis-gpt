"""Universal multi-step task orchestrator: plan -> execute (blackboard) -> synthesize.

This is the general engine for multi-step tasks ("многоходовки"). A bare ReAct loop —
where a weak local model improvises turn by turn and the goal drowns in a growing
transcript — is unreliable past one hop. The orchestrator instead:

  1. PLAN once: decompose the goal into a short ordered list of concrete steps.
  2. EXECUTE each step as a *focused* sub-task, writing its result to a blackboard and
     feeding the next step only the results it depends on (never the whole history).
  3. SYNTHESIZE the final answer from the blackboard.

Every stage fails safe: a malformed plan degrades to a single reasoning step, and a failed
step is recorded rather than fatal, so the engine always returns an answer. It is
domain-agnostic — web, documents, system and shopping are just tools it can call — which is
the whole point: one universal engine instead of a special pipe per domain.

The engine imports nothing from the agent; it takes the LLM and the tool runner as injected
async callables, so it is unit-testable with stubs and later reusable behind ``select_brain``
(frontier planning, local execution) without changes here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

# A short plan keeps a weak planner honest and bounds latency; raise only with evidence.
DEFAULT_MAX_STEPS = 6
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(s\d+)\s*\}\}")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class TaskStep:
    id: str
    goal: str
    kind: str  # "tool" | "reason"
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class StepResult:
    step_id: str
    title: str
    ok: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskPlan:
    goal: str
    steps: list[TaskStep]


@dataclass
class OrchestrationResult:
    ok: bool
    answer: str
    plan: TaskPlan
    results: list[StepResult]
    stopped_reason: str  # "completed" | "empty"


# Injected dependencies (structural typing — no agent import).
#   complete(messages) -> object with .ok: bool and .content: str
#   run_tool(name, arguments) -> object with .ok: bool, .summary: str, .data: dict
CompleteFn = Callable[[list[dict[str, str]]], Awaitable[Any]]
RunToolFn = Callable[[str, dict[str, Any]], Awaitable[Any]]
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def _planner_messages(
    goal: str,
    tool_specs: Sequence[tuple[str, str]],
    max_steps: int,
) -> list[dict[str, str]]:
    schema = (
        '{"steps":[{"id":"s1","goal":"...","kind":"tool|reason",'
        '"tool":"<tool-name-or-null>","arguments":{},"depends_on":[]}]}'
    )
    tools_block = "\n".join(f"- {name}: {desc}" for name, desc in tool_specs) or "- (нет)"
    # The placeholder token must reach the model literally as {{s1}}, so it lives in a
    # plain (non-f) string; everything interpolated is join-ed in below.
    rules = (
        "Правила: kind 'tool' — вызов ОДНОГО инструмента из списка (аргументы в arguments); "
        "kind 'reason' — фокусная под-задача мышления/письма без инструмента. "
        "Если для ответа нужны свежие факты, цены или данные из интернета — ОБЯЗАТЕЛЬНО "
        "добавь шаг 'tool', который их добывает, а не полагайся только на 'reason'. "
        "Ссылку на результат прошлого шага вставляй в строковый аргумент как {{s1}}. "
        "Только реально нужные шаги; зависимые ставь после тех, от кого зависят."
    )
    system = "\n".join(
        [
            f"Ты — планировщик задач. Разложи цель в упорядоченный список из 1..{max_steps} "
            "конкретных шагов. Выведи ТОЛЬКО валидный JSON по схеме, без пояснений:",
            schema,
            rules,
            "Доступные инструменты:",
            tools_block,
        ]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Цель: {goal}"},
    ]


def _coerce_steps(
    raw_steps: Any,
    *,
    allowed_tools: set[str],
    max_steps: int,
) -> list[TaskStep]:
    if not isinstance(raw_steps, list):
        return []
    steps: list[TaskStep] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps[:max_steps], start=1):
        if not isinstance(raw, dict):
            continue
        step_id = str(raw.get("id") or f"s{index}").strip() or f"s{index}"
        if step_id in seen:
            step_id = f"s{index}"
        goal = " ".join(str(raw.get("goal") or "").split())
        if not goal:
            continue
        kind = str(raw.get("kind") or "reason").strip().lower()
        tool = raw.get("tool")
        tool = str(tool).strip() if tool else None
        # Only honour a tool step when the tool actually exists; otherwise it degrades to
        # a reasoning step so a hallucinated tool name never dead-ends execution.
        if kind == "tool" and (not tool or tool not in allowed_tools):
            kind = "reason"
            tool = None
        if kind != "tool":
            kind = "reason"
            tool = None
        arguments = raw.get("arguments")
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        depends = raw.get("depends_on")
        depends = [str(d) for d in depends if str(d) in seen] if isinstance(depends, list) else []
        steps.append(
            TaskStep(id=step_id, goal=goal, kind=kind, tool=tool, arguments=arguments,
                     depends_on=depends)
        )
        seen.add(step_id)
    return steps


def parse_plan(
    content: str,
    goal: str,
    *,
    allowed_tools: set[str],
    max_steps: int = DEFAULT_MAX_STEPS,
) -> TaskPlan:
    """Parse the planner's JSON into a validated plan, or fall back to a single step.

    Never raises: a weak model that returns prose, broken JSON or nonsense still yields a
    usable one-step plan so the caller always has something to execute.
    """

    steps: list[TaskStep] = []
    match = _JSON_OBJECT_RE.search(content or "")
    if match:
        try:
            payload = json.loads(match.group(0))
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            steps = _coerce_steps(
                payload.get("steps"),
                allowed_tools=allowed_tools,
                max_steps=max_steps,
            )
    if not steps:
        steps = [TaskStep(id="s1", goal=goal, kind="reason")]
    return TaskPlan(goal=goal, steps=steps)


def _resolve_placeholders(value: Any, blackboard: dict[str, StepResult]) -> Any:
    """Replace {{sN}} references inside string arguments with prior step outputs."""

    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            prior = blackboard.get(m.group(1))
            return prior.output if prior is not None else m.group(0)

        return _PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, list):
        return [_resolve_placeholders(item, blackboard) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item, blackboard) for key, item in value.items()}
    return value


def _curated_context(step: TaskStep, blackboard: dict[str, StepResult]) -> str:
    parts: list[str] = []
    for dep in step.depends_on:
        prior = blackboard.get(dep)
        if prior is not None and prior.output:
            parts.append(f"[{dep}] {prior.output}")
    return "\n\n".join(parts)


class TaskOrchestrator:
    """Plan -> execute-with-blackboard -> synthesize, over injected LLM + tools."""

    def __init__(
        self,
        *,
        complete: CompleteFn,
        run_tool: RunToolFn,
        tool_specs: Sequence[tuple[str, str]],
        max_steps: int = DEFAULT_MAX_STEPS,
        emit: EmitFn | None = None,
        plan_complete: CompleteFn | None = None,
        fallback_query_tool: str | None = None,
    ) -> None:
        self._complete = complete
        # A tool that answers a plain {"query": goal}. When a plan produces no real
        # data (thin/failed plan), the engine runs this once so the answer is grounded
        # in evidence rather than the local planner's luck. None disables the backstop.
        self._fallback_query_tool = fallback_query_tool
        # Planning is the hard part; it may use a stronger brain (e.g. the frontier
        # model via select_brain) while execution stays on the local model. Falls back
        # to the execution brain when no dedicated planner is injected.
        self._plan_complete = plan_complete or complete
        self._run_tool = run_tool
        self._tool_specs = list(tool_specs)
        self._allowed_tools = {name for name, _ in self._tool_specs}
        # The backstop tool is deterministic (never planned), so it is allowed to run
        # even when it is not part of the planner's curated menu.
        if fallback_query_tool:
            self._allowed_tools.add(fallback_query_tool)
        self._max_steps = max(1, min(12, int(max_steps)))
        self._emit = emit

    async def _plan(self, goal: str) -> TaskPlan:
        content = ""
        try:
            messages = _planner_messages(goal, self._tool_specs, self._max_steps)
            result = await self._plan_complete(messages)
            content = getattr(result, "content", "") if getattr(result, "ok", False) else ""
        except Exception:  # noqa: BLE001 - planning must never crash the task
            content = ""
        return parse_plan(content, goal, allowed_tools=self._allowed_tools,
                          max_steps=self._max_steps)

    async def _run_reason_step(self, step: TaskStep, context: str, goal: str) -> StepResult:
        system = (
            "Выполни ОДНУ фокусную под-задачу в рамках общей цели. Дай только результат "
            "под-задачи — по существу, без вступлений и мета-комментариев."
        )
        user = f"Общая цель: {goal}\nПод-задача: {step.goal}"
        if context:
            user += f"\n\nЧто уже известно:\n{context}"
        try:
            result = await self._complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}]
            )
            output = getattr(result, "content", "") if getattr(result, "ok", False) else ""
        except Exception:  # noqa: BLE001 - a failed step is recorded, not fatal
            output = ""
        output = (output or "").strip()
        return StepResult(step_id=step.id, title=step.goal, ok=bool(output), output=output)

    async def _run_tool_step(
        self, step: TaskStep, blackboard: dict[str, StepResult]
    ) -> StepResult:
        arguments = _resolve_placeholders(step.arguments, blackboard)
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            response = await self._run_tool(str(step.tool), arguments)
            ok = bool(getattr(response, "ok", False))
            summary = str(getattr(response, "summary", "") or "")
            data = getattr(response, "data", {})
            data = dict(data) if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 - tool errors are recorded, not fatal
            ok, summary, data = False, f"{type(exc).__name__}: {exc}"[:400], {}
        return StepResult(step_id=step.id, title=step.goal, ok=ok, output=summary, data=data)

    async def _synthesize(self, goal: str, results: list[StepResult]) -> str:
        evidence = "\n\n".join(
            f"[{r.step_id}] {r.title}\n{r.output}" for r in results if r.output
        )
        if not evidence:
            return ""
        system = (
            "Собери итоговый ответ на цель пользователя из результатов выполненных шагов. "
            "Кратко, конкретно, по делу. Не выдумывай того, чего нет в результатах; если "
            "чего-то не хватает — честно скажи, чего именно."
        )
        user = f"Цель: {goal}\n\nРезультаты шагов:\n{evidence}"
        try:
            result = await self._complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}]
            )
            answer = getattr(result, "content", "") if getattr(result, "ok", False) else ""
        except Exception:  # noqa: BLE001 - fall back to the raw evidence
            answer = ""
        answer = (answer or "").strip()
        return answer or evidence

    async def _emit_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self._emit is not None:
            # Telemetry must never break the task.
            with suppress(Exception):
                await self._emit(kind, payload)

    async def run(self, goal: str) -> OrchestrationResult:
        goal = " ".join(str(goal or "").split())
        if not goal:
            empty = TaskPlan(goal="", steps=[])
            return OrchestrationResult(
                ok=False, answer="", plan=empty, results=[], stopped_reason="empty"
            )
        plan = await self._plan(goal)
        plan_steps = [{"id": s.id, "goal": s.goal, "kind": s.kind} for s in plan.steps]
        await self._emit_event("plan", {"goal": goal, "steps": plan_steps})
        blackboard: dict[str, StepResult] = {}
        results: list[StepResult] = []
        for step in plan.steps:
            if step.kind == "tool":
                result = await self._run_tool_step(step, blackboard)
            else:
                context = _curated_context(step, blackboard)
                result = await self._run_reason_step(step, context, goal)
            blackboard[step.id] = result
            results.append(result)
            await self._emit_event(
                "step",
                {"id": step.id, "goal": step.goal, "kind": step.kind, "ok": result.ok},
            )
        # Reliability backstop: if no tool step produced substantial data, run one
        # deterministic research pass on the goal so the answer is grounded in evidence.
        tool_step_ids = {step.id for step in plan.steps if step.kind == "tool"}
        grounded = any(
            r.ok and r.step_id in tool_step_ids and len(r.output.strip()) >= 80
            for r in results
        )
        if (
            not grounded
            and self._fallback_query_tool
            and self._fallback_query_tool in self._allowed_tools
        ):
            fallback_step = TaskStep(
                id="fallback",
                goal=goal,
                kind="tool",
                tool=self._fallback_query_tool,
                arguments={"query": goal, "limit": 5},
            )
            fallback = await self._run_tool_step(fallback_step, blackboard)
            blackboard["fallback"] = fallback
            results.append(fallback)
            await self._emit_event(
                "step",
                {"id": "fallback", "goal": goal, "kind": "tool", "ok": fallback.ok},
            )
        answer = await self._synthesize(goal, results)
        return OrchestrationResult(
            ok=bool(answer),
            answer=answer,
            plan=plan,
            results=results,
            stopped_reason="completed" if results else "empty",
        )
