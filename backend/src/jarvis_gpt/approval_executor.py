from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .learning import LearningEngine
from .llm import LLMRouter
from .models import ToolRunResponse
from .redaction import redact_text, redact_value
from .storage import JarvisStorage, utc_now
from .telemetry import TelemetryCollector
from .tools import ToolRegistry

MissionResumer = Callable[[dict[str, Any], ToolRunResponse], Awaitable[ToolRunResponse | None]]
MissionAborter = Callable[[dict[str, Any], str], Awaitable[ToolRunResponse | None]]
_T = TypeVar("_T")

SUPPORTED_APPROVAL_ACTIONS = (
    "dispatcher.start",
    "dispatcher.stop",
    "diagnostics.run",
    "learning.tick",
    "memory.save",
    "telemetry.snapshot",
    "tool.run",
)
_EXECUTIVE_MUTATION_TOOLS = frozenset({"execution.apply", "execution.transaction"})


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
        mission_aborter: MissionAborter | None = None,
    ) -> None:
        self.storage = storage
        self.llm = llm
        self.dispatcher = dispatcher
        self.tools = tools
        self.mission_resumer = mission_resumer
        self.mission_aborter = mission_aborter

    async def reconcile_interrupted_executions(self) -> list[dict[str, Any]]:
        """Fail closed and reconcile approvals interrupted by a previous runtime.

        Recovery never replays the approved action.  Approval rows are made
        terminal before callbacks run, and mission-bound callbacks have a
        durable pending marker so another cold start can retry reconciliation.
        """

        self.storage.recover_interrupted_approval_executions()
        return await self.reconcile_pending_approvals()

    async def reconcile_pending_approvals(
        self,
        *,
        approval_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Drain durable mission-reconciliation outbox rows without action replay."""

        reconciled: list[dict[str, Any]] = []
        for approval in self.storage.pending_approval_reconciliations():
            if approval_id is not None and approval.get("id") != approval_id:
                continue
            reconciliation = approval.get("result", {}).get("reconciliation", {})
            mode = reconciliation.get("mode") if isinstance(reconciliation, dict) else None
            if mode == "operator_rejected":
                reason = (
                    "[reconcile-only] operator rejected the approval; do not execute or "
                    "replay the original action"
                )
            elif mode == "operator_cancelled":
                reason = (
                    "[reconcile-only] operator cancelled the approval; do not execute or "
                    "replay the original action"
                )
            else:
                reason = (
                    "[reconcile-only] approval execution was interrupted by runtime restart; "
                    "inspect authoritative state and do not replay the original action"
                )

            async def reconcile_one(
                current: dict[str, Any] = approval,
                reconcile_reason: str = reason,
            ) -> dict[str, Any] | None:
                completed = await self._abort_mission(current, reconcile_reason)
                if not completed:
                    return None
                return self.storage.complete_approval_reconciliation(
                    str(current["id"]),
                    detail="mission branch reconciled without replay",
                )

            updated = await finish_despite_cancellation(reconcile_one())
            if updated is not None:
                reconciled.append(updated)
        return reconciled

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
        try:
            context_error = self._mission_context_error(payload, requested_action=action)
        except Exception as exc:
            context_error = redact_text(
                "Approval context could not be validated before its atomic claim: "
                f"{type(exc).__name__}: {exc}"
            )
        if context_error is not None:
            return ApprovalExecution(
                ok=False,
                summary=context_error,
                data={"approval": approval, "stale": True},
                approval=approval,
                status_code=409,
                finalize=False,
            )
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
                    "Approval execution was not claimed; " f"current status is {current_status}."
                ),
                data={"approval": current} if current is not None else {"approval_id": approval_id},
                approval=current,
                status_code=409 if current is not None else 404,
                finalize=False,
            )

        return await finish_despite_cancellation(
            self._execute_claimed(approval_id, claimed, action, payload)
        )

    async def _execute_claimed(
        self,
        approval_id: str,
        claimed: dict[str, Any],
        action: str,
        payload: dict[str, Any],
    ) -> ApprovalExecution:
        """Execute and finalize one claimed capability as one authoritative unit."""

        try:
            context_error = self._mission_context_error(payload, requested_action=action)
        except Exception as exc:
            context_error = redact_text(
                "Approval context could not be revalidated after its atomic claim: "
                f"{type(exc).__name__}: {exc}"
            )
        if context_error is not None:
            await self._abort_mission(
                claimed,
                (
                    "[reconcile-only] approval context changed after its atomic claim; "
                    f"do not execute the stale action: {context_error}"
                ),
            )
            result = ApprovalExecution(
                ok=False,
                summary=context_error,
                data={"stale": True, "post_claim_validation": False},
                status_code=409,
            )
        else:
            try:
                result = await self._execute_action(claimed, action, payload)
            except asyncio.CancelledError:
                exc = RuntimeError("approval action cancelled its own execution")
                await self._abort_mission(
                    claimed,
                    (
                        "[reconcile-only] approved action outcome is ambiguous; inspect current "
                        "state without replay: RuntimeError: approval action cancelled its own "
                        "execution"
                    ),
                )
                result = ApprovalExecution(
                    ok=False,
                    summary=f"Approval execution failed: RuntimeError: {exc}",
                    data={"error": "RuntimeError", "detail": str(exc)},
                )
            except Exception as exc:  # keep durable state out of a stuck executing state
                error_detail = redact_text(f"{type(exc).__name__}: {exc}")[:1000]
                await self._abort_mission(
                    claimed,
                    (
                        "[reconcile-only] approved action outcome is ambiguous; inspect current "
                        f"state without replay: {error_detail}"
                    ),
                )
                result = ApprovalExecution(
                    ok=False,
                    summary=f"Approval execution failed: {error_detail}",
                    data={"error": type(exc).__name__, "detail": error_detail},
                )

        safe_summary = redact_text(result.summary)[:2000]
        safe_data = redact_value(result.data)
        if not isinstance(safe_data, dict):
            safe_data = {"result": safe_data}
        result = ApprovalExecution(
            ok=result.ok,
            summary=safe_summary,
            data=safe_data,
            approval=result.approval,
            status_code=result.status_code,
            finalize=result.finalize,
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
        response = ApprovalExecution(
            ok=result.ok,
            summary=result.summary,
            data=result.data,
            approval=updated or claimed,
            status_code=result.status_code,
            finalize=True,
        )
        return response

    async def _abort_mission(self, approval: dict[str, Any], reason: str) -> bool:
        if self.mission_aborter is None:
            return False
        callback_error = "mission reconciliation callback returned no result"
        try:
            reconciled = await self.mission_aborter(approval, reason)
            if reconciled is not None:
                return True
        except Exception as exc:
            callback_error = redact_text(f"{type(exc).__name__}: {exc}")[:1000]
        payload = approval.get("payload")
        mission_id = (
            _optional_text(payload.get("mission_id")) if isinstance(payload, dict) else None
        )
        executive = getattr(self.tools, "executive", None)
        if mission_id and executive is not None:
            try:
                executive.terminate_mission(
                    mission_id,
                    reason=("approval reconciliation callback failed: " f"{callback_error}"),
                )
            except Exception:
                return False
            return True
        return False

    def _mission_context_error(
        self,
        payload: dict[str, Any],
        *,
        requested_action: str | None = None,
    ) -> str | None:
        mission_id = _optional_text(payload.get("mission_id"))
        task_id = _optional_text(payload.get("task_id"))
        if not mission_id and not task_id:
            return None
        if not mission_id or not task_id:
            return "Approval mission context is incomplete and cannot be executed safely."
        if requested_action is not None and requested_action != "tool.run":
            return (
                "Executive mission approvals are executable only through the canonical "
                "tool.run approval action."
            )
        tool_name = str(payload.get("tool") or "").strip()
        if tool_name not in _EXECUTIVE_MUTATION_TOOLS:
            return (
                "Executive missions accept mutations only through contract-bound "
                "execution.apply/execution.transaction; wrapper, native, browser, "
                "and dispatcher "
                "actions are not executable from a mission approval."
            )
        mission = self.storage.get_mission(mission_id)
        if mission is None:
            return "Approval is stale: its mission no longer exists."
        task = next(
            (item for item in mission.get("tasks", []) if item.get("id") == task_id),
            None,
        )
        if task is None:
            return "Approval is stale: its mission task no longer exists."
        if task.get("status") != "blocked":
            return (
                "Approval is stale or not ready: the bound mission task must still be blocked "
                "on this approval."
            )
        executive = getattr(self.tools, "executive", None)
        if executive is None:
            return "Approval is stale: its executive coordinator is unavailable."
        snapshot = executive.snapshot(mission_id)
        if snapshot is None:
            return "Approval is stale: its durable executive plan is unavailable."
        planner = snapshot.get("planner")
        task_map = snapshot.get("task_map")
        if not isinstance(planner, dict) or not isinstance(task_map, dict):
            return "Approval is stale: the executive plan is malformed."
        claim = payload.get("executive_claim")
        if not isinstance(claim, dict):
            return "Approval is stale: it is not bound to an executive step attempt."
        step_id = str(claim.get("step_id") or "")
        if (
            claim.get("protocol") != "jarvis.executive-approval.v1"
            or claim.get("mission_id") != mission_id
            or claim.get("task_id") != task_id
            or str(task_map.get(step_id) or "") != task_id
            or claim.get("plan_revision") != planner.get("revision")
        ):
            return "Approval is stale: its executive plan revision no longer matches."
        persisted_environment = planner.get("environment")
        persisted_digest = (
            persisted_environment.get("digest") if isinstance(persisted_environment, dict) else None
        )
        current_environment = getattr(executive, "environment", None)
        current_digest = getattr(current_environment, "digest", None)
        if (
            not isinstance(persisted_digest, str)
            or not isinstance(current_digest, str)
            or claim.get("environment_digest") != persisted_digest
            or current_digest != persisted_digest
        ):
            return "Approval is stale: its environment fingerprint no longer matches."
        steps = planner.get("steps")
        step = (
            next(
                (
                    item
                    for item in steps
                    if isinstance(item, dict)
                    and isinstance(item.get("spec"), dict)
                    and item["spec"].get("step_id") == step_id
                ),
                None,
            )
            if isinstance(steps, list)
            else None
        )
        if (
            not isinstance(step, dict)
            or step.get("status") not in {"running", "verifying"}
            or claim.get("step_attempt") != step.get("attempts")
        ):
            return "Approval is stale: its executive step attempt is no longer active."
        contract = step.get("verification_contract")
        if not isinstance(contract, dict) or claim.get("verification_contract") != contract:
            return (
                "Approval is stale: its action postcondition contract no longer "
                "matches the executive step."
            )
        arguments = payload.get("arguments")
        validator = getattr(executive, "action_contract_matches", None)
        if (
            not isinstance(arguments, dict)
            or not callable(validator)
            or not validator(
                mission_id,
                task_id,
                tool=tool_name,
                arguments=arguments,
                expected_contract=contract,
            )
        ):
            return (
                "Approval is stale: its exact execution arguments do not match "
                "the bound action postcondition contract."
            )
        return None

    async def _execute_action(
        self,
        approval: dict[str, Any],
        action: str,
        payload: dict[str, Any],
    ) -> ApprovalExecution:
        if action == "dispatcher.start":
            result = await asyncio.to_thread(self.dispatcher.run_compose_verified, "up")
            return _compose_result("Dispatcher start requested.", result)
        if action == "dispatcher.stop":
            result = await asyncio.to_thread(self.dispatcher.run_compose_verified, "down")
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
                    "Learning tick executed from approval: " f"{result['lesson_count']} lesson(s)."
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
                    # The approved side effect is terminally successful even when
                    # the mission immediately reaches another independent gate.
                    # Mission continuation status is reported separately and must
                    # never make the already executed action look retryable.
                    ok = response.ok
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


async def finish_despite_cancellation(awaitable: Awaitable[_T]) -> _T:
    """Finish one authoritative callback, then preserve caller cancellation.

    Rejection/cancellation is already durable before its mission callback runs.
    Shielding prevents a disconnected HTTP client or Ctrl+C from stranding the
    executive branch, while the caller still observes cancellation afterwards.
    Repeated cancellation requests are drained until the callback is terminal.
    """

    task = asyncio.ensure_future(awaitable)
    current = asyncio.current_task()
    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_requested = True
            if current is not None:
                current.uncancel()
    if cancellation_requested:
        raise asyncio.CancelledError
    return result
