from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .learning import LearningEngine
from .llm import LLMRouter
from .storage import JarvisStorage, utc_now
from .telemetry import TelemetryCollector
from .tools import ToolRegistry


@dataclass(frozen=True)
class ApprovalExecution:
    ok: bool
    summary: str
    data: dict[str, Any]
    approval: dict[str, Any] | None = None
    status_code: int = 200
    finalize: bool = True


class ApprovalExecutor:
    def __init__(
        self,
        *,
        storage: JarvisStorage,
        llm: LLMRouter,
        dispatcher: DispatcherManager,
        tools: ToolRegistry,
    ) -> None:
        self.storage = storage
        self.llm = llm
        self.dispatcher = dispatcher
        self.tools = tools

    async def execute(self, approval_id: str) -> ApprovalExecution:
        approval = self.storage.get_approval(approval_id)
        if approval is None:
            return ApprovalExecution(
                ok=False,
                summary="Approval not found.",
                data={"approval_id": approval_id},
                status_code=404,
                finalize=False,
            )
        if approval["status"] != "approved":
            summary = (
                "Approval must be approved before execution; "
                f"current status is {approval['status']}."
            )
            return ApprovalExecution(
                ok=False,
                summary=summary,
                data={"approval": approval},
                approval=approval,
                status_code=409,
                finalize=False,
            )

        action = str(approval["requested_action"])
        payload = approval.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        result = await self._execute_action(action, payload)
        if not result.finalize:
            return result

        updated = self.storage.update_approval(
            approval_id,
            status="executed",
            result={
                "ok": result.ok,
                "summary": result.summary,
                "data": result.data,
                "executed_at": utc_now(),
            },
        )
        return ApprovalExecution(
            ok=result.ok,
            summary=result.summary,
            data=result.data,
            approval=updated or approval,
            status_code=result.status_code,
            finalize=True,
        )

    async def _execute_action(self, action: str, payload: dict[str, Any]) -> ApprovalExecution:
        if action == "dispatcher.start":
            result = self.dispatcher.run_compose("up")
            return _compose_result("Dispatcher start requested.", result)
        if action == "dispatcher.stop":
            result = self.dispatcher.run_compose("down")
            return _compose_result("Dispatcher stop requested.", result)
        if action == "diagnostics.run":
            diagnostics = await run_diagnostics(
                settings=self.tools.settings,
                storage=self.storage,
                llm=self.llm,
            )
            return ApprovalExecution(
                ok=diagnostics.ok,
                summary="Diagnostics executed from approval.",
                data=diagnostics.model_dump(),
            )
        if action == "learning.tick":
            limit = _int_value(payload.get("limit"), default=20, minimum=5, maximum=100)
            result = LearningEngine(self.storage).tick(limit=limit)
            return ApprovalExecution(
                ok=True,
                summary=(
                    "Learning tick executed from approval: "
                    f"{result['lesson_count']} lesson(s)."
                ),
                data=result,
            )
        if action == "telemetry.snapshot":
            snapshot = TelemetryCollector(self.tools.settings).snapshot()
            if bool(payload.get("persist", True)):
                self.storage.record_telemetry(snapshot)
            return ApprovalExecution(
                ok=True,
                summary="Telemetry snapshot executed from approval.",
                data=snapshot,
            )
        if action == "memory.save":
            content = str(payload.get("content") or "").strip()
            if not content:
                return ApprovalExecution(
                    ok=False,
                    summary="memory.save approval payload requires content.",
                    data={"payload": payload},
                )
            tags = payload.get("tags") or ["approval"]
            if isinstance(tags, str):
                tags = [item.strip() for item in tags.split(",") if item.strip()]
            if not isinstance(tags, list):
                tags = ["approval"]
            memory = self.storage.add_memory(
                content=content,
                namespace=str(payload.get("namespace") or "operator")[:80],
                tags=[str(tag)[:80] for tag in tags[:12]],
                importance=_float_value(
                    payload.get("importance"),
                    default=0.65,
                    minimum=0.0,
                    maximum=1.0,
                ),
            )
            return ApprovalExecution(
                ok=True,
                summary="Memory saved from approval.",
                data={"memory": memory},
            )
        if action == "tool.run":
            tool_name = str(payload.get("tool") or "").strip()
            arguments = payload.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            spec = self.tools.get(tool_name)
            if spec is None:
                return ApprovalExecution(
                    ok=False,
                    summary=f"Tool {tool_name!r} is not registered.",
                    data={"tool": tool_name},
                )
            response = await self.tools.run(tool_name, arguments, allow_danger=True)
            return ApprovalExecution(
                ok=response.ok,
                summary=response.summary,
                data={"tool_run": response.model_dump()},
            )

        return ApprovalExecution(
            ok=False,
            summary=f"Unsupported approval action: {action}",
            data={
                "requested_action": action,
                "supported_actions": [
                    "dispatcher.start",
                    "dispatcher.stop",
                    "diagnostics.run",
                    "learning.tick",
                    "memory.save",
                    "telemetry.snapshot",
                    "tool.run",
                ],
            },
            status_code=400,
            finalize=False,
        )


def _compose_result(summary: str, result: dict[str, Any]) -> ApprovalExecution:
    return ApprovalExecution(
        ok=bool(result.get("ok")),
        summary=summary if result.get("ok") else str(result.get("summary") or summary),
        data=result,
    )


def _int_value(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _float_value(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
