from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .learning import LearningEngine
from .llm import LLMRouter
from .models import ToolRunResponse
from .storage import JarvisStorage, utc_now
from .telemetry import TelemetryCollector
from .tools import ToolRegistry

MissionResumer = Callable[[dict[str, Any], ToolRunResponse], Awaitable[ToolRunResponse | None]]

SUPPORTED_APPROVAL_ACTIONS = (
    "dispatcher.start",
    "dispatcher.stop",
    "diagnostics.run",
    "learning.tick",
    "memory.save",
    "telemetry.snapshot",
    "tool.run",
)


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
        mission_resumer: MissionResumer | None = None,
    ) -> None:
        self.storage = storage
        self.llm = llm
        self.dispatcher = dispatcher
        self.tools = tools
        self.mission_resumer = mission_resumer

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
        # Validate before claiming so malformed/unsupported legacy approvals stay
        # reviewable instead of becoming permanently terminal without an action.
        if action not in SUPPORTED_APPROVAL_ACTIONS:
            return await self._execute_action(approval, action, payload)

        claimed = self.storage.claim_approval_execution(approval_id)
        if claimed is None:
            current = self.storage.get_approval(approval_id)
            current_status = current["status"] if current is not None else "missing"
            return ApprovalExecution(
                ok=False,
                summary=(
                    "Approval execution was not claimed; "
                    f"current status is {current_status}."
                ),
                data={"approval": current} if current is not None else {"approval_id": approval_id},
                approval=current,
                status_code=409 if current is not None else 404,
                finalize=False,
            )

        try:
            result = await self._execute_action(claimed, action, payload)
        except asyncio.CancelledError:
            self.storage.finalize_approval_execution(
                approval_id,
                status="failed",
                result={
                    "ok": False,
                    "summary": "Approval execution was cancelled.",
                    "data": {"error": "CancelledError"},
                    "executed_at": utc_now(),
                },
            )
            raise
        except Exception as exc:  # keep the durable approval out of a stuck executing state
            result = ApprovalExecution(
                ok=False,
                summary=f"Approval execution failed: {type(exc).__name__}: {exc}",
                data={"error": type(exc).__name__, "detail": str(exc)},
            )

        terminal_status = "executed" if result.ok and result.finalize else "failed"
        terminal_result = {
            "ok": result.ok,
            "summary": result.summary,
            "data": result.data,
            "executed_at": utc_now(),
        }
        updated = self.storage.finalize_approval_execution(
            approval_id,
            status=terminal_status,
            result=terminal_result,
        )
        if updated is None:
            updated = self.storage.get_approval(approval_id)
        return ApprovalExecution(
            ok=result.ok,
            summary=result.summary,
            data=result.data,
            approval=updated or claimed,
            status_code=result.status_code,
            finalize=True,
        )

    async def _execute_action(
        self,
        approval: dict[str, Any],
        action: str,
        payload: dict[str, Any],
    ) -> ApprovalExecution:
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
            result = await LearningEngine(self.storage, llm=self.llm).tick_async(limit=limit)
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
            mission_id = _optional_text(payload.get("mission_id"))
            task_id = _optional_text(payload.get("task_id"))
            response = await self.tools.run(
                tool_name,
                arguments,
                allow_danger=True,
                mission_id=mission_id,
                task_id=task_id,
            )
            data: dict[str, Any] = {"tool_run": response.model_dump()}
            ok = response.ok
            summary = response.summary
            if self.mission_resumer is not None and mission_id and task_id:
                resume_result = await self.mission_resumer(approval, response)
                if resume_result is not None:
                    data["mission_resume"] = resume_result.model_dump()
                    ok = response.ok and resume_result.ok
                    summary = (
                        f"{response.summary} Mission resumed: {resume_result.summary}"
                        if resume_result.summary
                        else response.summary
                    )
            return ApprovalExecution(
                ok=ok,
                summary=summary[:2000],
                data=data,
            )

        return ApprovalExecution(
            ok=False,
            summary=f"Unsupported approval action: {action}",
            data={
                "requested_action": action,
                "supported_actions": list(SUPPORTED_APPROVAL_ACTIONS),
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


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
