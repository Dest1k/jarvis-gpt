from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .cognitive_memory import ExecutionPlaybookStore
from .execution_protocol import ActionClass, canonical_action_json, classify_payload, parse_action
from .executive_planner import (
    ActionCall,
    AdaptiveDAGPlanner,
    AssertionCriterion,
    AssertionResult,
    DecompositionProposal,
    EnvironmentFingerprint,
    GoalDefinition,
    GoalStatus,
    PlannerLimits,
    PlannerSnapshot,
    PlanRevision,
    PreconditionFingerprint,
    StepSpec,
    StepStatus,
    VerificationContract,
)
from .models import ToolRunResponse
from .state_verification import (
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)
from .storage import JarvisStorage

EXECUTIVE_PROTOCOL = "jarvis.executive.v1"
MISSION_DECOMPOSITION_PROTOCOL = "jarvis.mission-decomposition.v1"
_KEY_PREFIX = "executive.plan."
_MISSING = object()
_STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EXECUTIVE_MUTATION_TOOLS = frozenset({"execution.apply", "execution.transaction"})


@dataclass(frozen=True, slots=True)
class ExecutiveStepClaim:
    step_id: str
    task: dict[str, Any]
    planner: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutiveStepOutcome:
    step_id: str
    verified: bool
    adapted: bool
    planner: dict[str, Any]
    added_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionDecompositionStep:
    step_id: str
    title: str
    objective: str
    dependencies: tuple[str, ...]
    assertion: str
    evidence_policy: str


@dataclass(frozen=True, slots=True)
class MissionDecomposition:
    protocol: str
    steps: tuple[MissionDecompositionStep, ...]
    rationale: str


@dataclass(frozen=True, slots=True)
class TrustedInspectorEvidence:
    """Runtime-minted, plan-bound evidence from an independent inspector.

    The issuer object is intentionally process-local.  A model-produced mapping,
    a nested tool payload, or a reconstructed dataclass cannot cross this trust
    boundary; only ``ExecutiveCoordinator.capture_inspector_evidence`` and the
    coordinator's deterministic inspectors can mint an accepted value.
    """

    _issuer: object
    protocol: str
    mission_id: str
    task_id: str
    step_id: str
    plan_revision: int
    step_attempt: int
    environment_digest: str
    planned_action_digest: str
    verification_contract_sha256: str | None
    outcome_tool: str
    action_tool: str
    action_id: str
    action_kind: str
    inspector: str
    ok: bool
    status: str
    summary: str
    evidence: tuple[dict[str, Any], ...]


class ExecutiveCoordinator:
    """Persisted bridge between mission rows and the deterministic DAG planner."""

    def __init__(
        self,
        *,
        storage: JarvisStorage,
        host_profile: dict[str, Any],
        playbooks: ExecutionPlaybookStore | None = None,
        recover_interrupted: bool = False,
    ) -> None:
        self.storage = storage
        self.playbooks = playbooks
        self._host_profile = _profile_copy(host_profile)
        self._environment = _environment_from_profile(self._host_profile)
        self._lock = threading.RLock()
        self._inspector_issuer = object()
        # Recovery mutates durable mission state and therefore belongs only to
        # the designated primary runtime.  CLI/read-only AgentRuntime instances
        # use the default False value and cannot steal work from a live server.
        if recover_interrupted:
            self._recover_interrupted_plans()

    @property
    def environment(self) -> EnvironmentFingerprint:
        return self._environment.model_copy(deep=True)

    def create_for_mission(
        self,
        mission: dict[str, Any],
        *,
        decomposition: MissionDecomposition | None = None,
    ) -> dict[str, Any]:
        mission_id = str(mission.get("id") or "")
        goal_text = str(mission.get("goal") or "").strip()
        tasks = mission.get("tasks")
        if not mission_id or not goal_text or not isinstance(tasks, list) or not tasks:
            raise ValueError("persisted mission with at least one task is required")
        with self._lock:
            playbook_hits = self._lookup_playbooks(goal_text)
            goal = GoalDefinition(
                goal_id=mission_id,
                objective=goal_text,
                criteria=(
                    AssertionCriterion(
                        assertion_id="goal.result",
                        description="All DAG steps passed their independent assertions.",
                        inspector="mission.goal.result",
                        expected={"all_steps_verified": True},
                    ),
                ),
                context={
                    "mission_title": str(mission.get("title") or "")[:240],
                    "host_profile_sha256": self._environment.facts["profile_fingerprint"],
                    "playbooks": playbook_hits,
                },
            )
            planner = AdaptiveDAGPlanner(
                goal=goal,
                environment=self._environment,
                limits=PlannerLimits(
                    max_steps=256,
                    max_revisions=16,
                    max_total_attempts=512,
                    max_step_attempts=3,
                ),
            )
            specs, task_map = _initial_specs(
                tasks,
                self._environment,
                goal=goal_text,
                decomposition=decomposition,
            )
            planner.load_decomposition(
                DecompositionProposal(
                    goal_id=mission_id,
                    steps=specs,
                    rationale=(
                        decomposition.rationale
                        if decomposition is not None
                        else "Deterministic task-specific decomposition with explicit verification."
                    ),
                )
            )
            record = self._record(mission_id, planner, task_map, playbook_hits)
            self._persist(mission_id, record)
            return record

    def ensure_for_mission(self, mission: dict[str, Any]) -> dict[str, Any]:
        """Return an existing plan or deterministically repair the create crash window.

        A mission row with only its original pending tasks is the sole safe
        backfill case.  Any evidence of prior execution without a durable DAG is
        quarantined instead of falling through to the legacy FIFO executor.
        """

        mission_id = str(mission.get("id") or "")
        if not mission_id:
            raise ValueError("persisted mission id is required")
        with self._lock:
            raw = self.storage.get_runtime_value(_KEY_PREFIX + mission_id, _MISSING)
            if raw is not _MISSING:
                loaded = self._load(mission_id)
                if loaded is None:
                    self._fail_closed_unplanned_mission(
                        mission,
                        "executive plan record is malformed or uses an unknown protocol",
                    )
                    raise RuntimeError("mission executive plan is malformed")
                planner, record = loaded
                return self._record(
                    mission_id,
                    planner,
                    dict(record["task_map"]),
                    list(record.get("playbooks") or []),
                )

            tasks = mission.get("tasks")
            clean_create_window = bool(
                mission.get("status") == "planned"
                and isinstance(tasks, list)
                and tasks
                and all(item.get("status") == "pending" for item in tasks)
            )
            if clean_create_window:
                return self.create_for_mission(mission)
            self._fail_closed_unplanned_mission(
                mission,
                "mission has execution state but no durable executive plan",
            )
            raise RuntimeError("mission has no recoverable executive plan")

    def capture_inspector_evidence(
        self,
        mission_id: str,
        task_id: str,
        action_result: ToolRunResponse,
        *,
        outcome_tool: str | None = None,
        action_arguments: dict[str, Any] | None = None,
        read_only: bool = False,
    ) -> TrustedInspectorEvidence:
        """Mint trusted evidence from a direct, typed state-verifier result.

        Callers must invoke this at the direct tool boundary.  Only the top-level
        ``verification`` field is inspected; recursive/nested payloads and generic
        LLM verdicts are deliberately ineligible.
        """

        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                raise KeyError(f"executive plan is missing for mission {mission_id}")
            planner, record = loaded
            step_id = next(
                (step for step, mapped in dict(record["task_map"]).items() if mapped == task_id),
                None,
            )
            if step_id is None:
                raise KeyError(f"mission task {task_id} is not mapped to the DAG")
            current = _step_snapshot(planner, step_id)
            if current.status not in {StepStatus.RUNNING, StepStatus.VERIFYING}:
                raise ValueError("inspector evidence requires the active DAG step")
            reconciliation = action_result.tool == "execution.verify"
            if reconciliation:
                reconciliation_args = action_arguments or {}
                source_tool = str(reconciliation_args.get("source_tool") or "")
                source_arguments = reconciliation_args.get("arguments")
                if (
                    current.spec.evidence_policy != "state"
                    or current.spec.action.tool != "execution.verify"
                    or current.spec.action.arguments != reconciliation_args
                    or source_tool not in _EXECUTIVE_MUTATION_TOOLS
                    or not isinstance(source_arguments, dict)
                    or action_result.data.get("source_tool") != source_tool
                ):
                    raise ValueError(
                        "reconciliation inspection is not the exact durable no-replay action"
                    )
                strict_verification = (
                    _strict_transaction_verification(
                        action_result.data.get("verification"),
                        source_arguments,
                    )
                    if source_tool == "execution.transaction"
                    else _strict_verification_result(
                        action_result.data.get("verification")
                    )
                )
                reconciliation_identity = _expected_action_identity(
                    source_tool,
                    source_arguments,
                )
                if strict_verification is not None and reconciliation_identity != (
                    strict_verification.action_id,
                    strict_verification.action_kind,
                ):
                    strict_verification = None
            else:
                strict_verification = (
                    _strict_transaction_verification(
                        action_result.data.get("verification"),
                        action_arguments or {},
                    )
                    if action_result.tool == "execution.transaction"
                    else _strict_verification_result(action_result.data.get("verification"))
                )
            verification = strict_verification
            inspector = "state_reconciliation_verifier" if reconciliation else "state_verifier"
            if (
                read_only
                and not reconciliation
                and action_result.tool not in _EXECUTIVE_MUTATION_TOOLS
            ):
                scoped_verification = _inspect_read_only_artifact(
                    action_result,
                    action_arguments or {},
                    spec=current.spec,
                )
                if strict_verification is not None and scoped_verification is not None:
                    verification = _combine_read_only_verification(
                        strict_verification,
                        scoped_verification,
                    )
                    inspector = "state_verifier+read_only_scope"
                else:
                    verification = scoped_verification
                    inspector = "read_only_artifact_inspector"
            if verification is None:
                raise ValueError(
                    "direct typed state-verifier or read-only artifact evidence is required"
                )
            if verification.ok and not action_result.ok:
                raise ValueError(
                    "successful inspector evidence requires a successful action result"
                )
            expected_action = _expected_action_identity(
                action_result.tool,
                action_arguments or {},
            )
            if action_result.tool in _EXECUTIVE_MUTATION_TOOLS and expected_action is None:
                raise ValueError(
                    f"{action_result.tool} inspector evidence requires action binding"
                )
            if expected_action is not None and expected_action != (
                verification.action_id,
                verification.action_kind,
            ):
                raise ValueError("inspector evidence does not match the requested action")
            if action_result.tool in _EXECUTIVE_MUTATION_TOOLS:
                contract = _verification_contract(
                    current.spec,
                    action_result.tool,
                    action_arguments or {},
                )
                if (
                    contract is None
                    or current.verification_contract is None
                    or contract != current.verification_contract
                ):
                    raise ValueError(
                        "inspector evidence does not match the bound step postcondition"
                    )
            snapshot = planner.snapshot()
            return self._mint_evidence(
                mission_id=mission_id,
                task_id=task_id,
                step_id=step_id,
                plan_revision=snapshot.revision,
                step_attempt=current.attempts,
                planned_action_digest=_planned_action_digest(
                    current.spec,
                    current.bound_action,
                ),
                verification_contract_sha256=_contract_digest(current.verification_contract),
                outcome_tool=outcome_tool or action_result.tool,
                action_tool=action_result.tool,
                verification=verification,
                inspector=inspector,
            )

    def capture_cognitive_evidence(
        self,
        mission_id: str,
        task_id: str,
        result: ToolRunResponse,
    ) -> TrustedInspectorEvidence:
        """Inspect a model-produced artifact structurally without accepting its claims.

        The model never supplies the binding fields.  ``AgentRuntime`` attaches
        them after generation, and this independent inspector compares them with
        the durable active DAG before accepting only artifact existence, scope,
        and integrity.  It does not interpret a nested model verification verdict.
        """

        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                raise KeyError(f"executive plan is missing for mission {mission_id}")
            planner, record = loaded
            step_id = next(
                (step for step, mapped in dict(record["task_map"]).items() if mapped == task_id),
                None,
            )
            if step_id is None:
                raise KeyError(f"mission task {task_id} is not mapped to the DAG")
            current = _step_snapshot(planner, step_id)
            if current.status not in {StepStatus.RUNNING, StepStatus.VERIFYING}:
                raise ValueError("cognitive evidence requires the active DAG step")
            if current.spec.evidence_policy != "artifact":
                raise ValueError(
                    "cognitive evidence is permitted only for planner-declared "
                    "artifact-only assertions"
                )
            artifact = result.data.get("executive_artifact")
            if not isinstance(artifact, dict) or set(artifact) != {
                "protocol",
                "goal",
                "task_title",
                "objective_sha256",
                "assertion_sha256",
                "summary_sha256",
            }:
                raise ValueError("typed executive artifact binding is required")
            summary = result.summary.strip()
            goal = planner.snapshot().goal.objective
            objective_sha256 = _objective_digest(current.spec)
            assertion_sha256 = _assertion_digest(current.spec)
            expected_terms, matched_terms = _artifact_relevance(
                current.spec,
                summary,
            )
            passed = bool(
                result.ok
                and result.tool == "mission.execute_next"
                and len(summary) >= 20
                and artifact.get("protocol") == "jarvis.cognitive-artifact.v1"
                and artifact.get("goal") == goal
                and artifact.get("task_title") == current.spec.title
                and artifact.get("objective_sha256") == objective_sha256
                and artifact.get("assertion_sha256") == assertion_sha256
                and artifact.get("summary_sha256") == _json_sha256(summary)
                and _has_required_relevance(expected_terms, matched_terms)
            )
            verification = VerificationResult(
                ok=passed,
                status=(VerificationStatus.PASSED if passed else VerificationStatus.FAILED),
                action_id=f"cognitive:{task_id}:{current.attempts}",
                action_kind="deterministic.cognitive_artifact",
                summary="Deterministic cognitive artifact scope and integrity inspection.",
                evidence=(
                    VerificationEvidence(
                        source="executive.cognitive_artifact",
                        assertion=(
                            "artifact is non-empty, bound to the active goal and step, and "
                            "deterministically relevant to its assertion"
                        ),
                        expected={
                            "goal": goal,
                            "task_title": current.spec.title,
                            "objective_sha256": objective_sha256,
                            "assertion_sha256": assertion_sha256,
                            "minimum_relevant_terms": _required_relevance_count(expected_terms),
                        },
                        observed={
                            "goal": artifact.get("goal"),
                            "task_title": artifact.get("task_title"),
                            "objective_sha256": artifact.get("objective_sha256"),
                            "assertion_sha256": artifact.get("assertion_sha256"),
                            "summary_sha256": artifact.get("summary_sha256"),
                            "summary_length": len(summary),
                            "matched_terms": sorted(matched_terms),
                        },
                        passed=passed,
                        captured_at=datetime.now(UTC).isoformat(),
                        subject=task_id,
                    ),
                ),
            )
            snapshot = planner.snapshot()
            return self._mint_evidence(
                mission_id=mission_id,
                task_id=task_id,
                step_id=step_id,
                plan_revision=snapshot.revision,
                step_attempt=current.attempts,
                planned_action_digest=_planned_action_digest(
                    current.spec,
                    current.bound_action,
                ),
                verification_contract_sha256=_contract_digest(current.verification_contract),
                outcome_tool=result.tool,
                action_tool=result.tool,
                verification=verification,
                inspector="cognitive_artifact_inspector",
            )

    def bind_action_contract(
        self,
        mission_id: str,
        task_id: str,
        *,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist an exact mutation/postcondition contract before execution."""

        if tool not in _EXECUTIVE_MUTATION_TOOLS:
            return None
        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                raise KeyError(f"executive plan is missing for mission {mission_id}")
            planner, record = loaded
            task_map = dict(record["task_map"])
            step_id = next(
                (step for step, mapped in task_map.items() if mapped == task_id),
                None,
            )
            if step_id is None:
                raise KeyError(f"mission task {task_id} is not mapped to the DAG")
            current = _step_snapshot(planner, step_id)
            if current.spec.action.tool == "execution.verify":
                raise ValueError(
                    "reconcile-only steps may inspect the exact prior postcondition but "
                    "cannot execute a mutation"
                )
            canonical_payloads = _canonical_mutation_payloads(tool, arguments)
            if not canonical_payloads or not _mutation_actions_are_plan_bound(
                planner,
                current.spec,
                tool,
                canonical_payloads,
            ):
                raise ValueError(
                    f"{tool} action subject is not bound to the step objective "
                    "or trusted predecessor evidence"
                )
            if not all(
                _process_postcondition_is_plan_bound(
                    current.spec,
                    canonical_payload,
                    arguments.get("verification"),
                )
                for canonical_payload in canonical_payloads
            ):
                raise ValueError(
                    "process postcondition is not bound to the step objective and action"
                )
            contract = _verification_contract(current.spec, tool, arguments)
            if contract is None:
                raise ValueError(
                    f"{tool} requires a concrete action postcondition contract"
                )
            planner.bind_verification_contract(
                step_id,
                contract,
                action=ActionCall(
                    tool=tool,
                    arguments=arguments,
                    destructive=True,
                ),
            )
            saved = self._record(
                mission_id,
                planner,
                task_map,
                list(record.get("playbooks") or []),
            )
            self._persist(mission_id, saved)
            return contract.model_dump(mode="json")

    def action_contract_matches(
        self,
        mission_id: str,
        task_id: str,
        *,
        tool: str,
        arguments: dict[str, Any],
        expected_contract: dict[str, Any],
    ) -> bool:
        """Recompute an approval capability from its exact execution payload.

        Persisted claims are not bearer tokens for arbitrary arguments.  This
        comparison is deliberately repeated immediately before and after the
        approval's atomic execution claim.
        """

        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                return False
            planner, record = loaded
            step_id = next(
                (step for step, mapped in dict(record["task_map"]).items() if mapped == task_id),
                None,
            )
            if step_id is None:
                return False
            current = _step_snapshot(planner, step_id)
            if current.status not in {StepStatus.RUNNING, StepStatus.VERIFYING}:
                return False
            try:
                expected = VerificationContract.model_validate(expected_contract)
            except (TypeError, ValueError):
                return False
            actual = _verification_contract(current.spec, tool, arguments)
            actual_action = ActionCall(
                tool=tool,
                arguments=arguments,
                destructive=True,
            )
            return bool(
                actual is not None
                and current.bound_action == actual_action
                and current.verification_contract is not None
                and expected == current.verification_contract
                and actual == expected
            )

    def cognitive_artifact_binding(
        self,
        mission_id: str,
        task_id: str,
    ) -> dict[str, str]:
        """Return runtime-owned scope fields for a cognitive step artifact."""

        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                raise KeyError(f"executive plan is missing for mission {mission_id}")
            planner, record = loaded
            step_id = next(
                (step for step, mapped in dict(record["task_map"]).items() if mapped == task_id),
                None,
            )
            if step_id is None:
                raise KeyError(f"mission task {task_id} is not mapped to the DAG")
            current = _step_snapshot(planner, step_id)
            if current.status not in {StepStatus.RUNNING, StepStatus.VERIFYING}:
                raise ValueError("cognitive artifact binding requires the active DAG step")
            if current.spec.evidence_policy != "artifact":
                raise ValueError(
                    "cognitive artifact binding is unavailable for observation/state steps"
                )
            return {
                "protocol": "jarvis.cognitive-artifact.v1",
                "goal": planner.snapshot().goal.objective,
                "task_title": current.spec.title,
                "objective_sha256": _objective_digest(current.spec),
                "assertion_sha256": _assertion_digest(current.spec),
            }

    def approval_claim_reconciled(
        self,
        mission_id: str,
        task_id: str,
        claim: dict[str, Any],
    ) -> bool:
        """Recognize a claim already retired by a reconcile-only DAG revision.

        Approval outbox delivery is at-least-once and can occur after cold-start
        planner recovery.  This check makes the second delivery idempotent without
        treating an unrelated revision or task replacement as reconciliation.
        """

        if (
            claim.get("protocol") != "jarvis.executive-approval.v1"
            or claim.get("mission_id") != mission_id
            or claim.get("task_id") != task_id
            or not isinstance(claim.get("step_id"), str)
            or not isinstance(claim.get("plan_revision"), int)
        ):
            return False
        step_id = str(claim["step_id"])
        claimed_revision = int(claim["plan_revision"])
        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                return False
            planner, record = loaded
            snapshot = planner.snapshot()
            if (
                snapshot.revision <= claimed_revision
                or str(record["task_map"].get(step_id) or "") == task_id
                or any(item.spec.step_id == step_id for item in snapshot.steps)
            ):
                return False
            return any(
                revision.revision > claimed_revision
                and step_id in revision.removed
                and revision.reason.startswith("[reconcile-only]")
                for revision in snapshot.revision_history
            )

    def _mint_evidence(
        self,
        *,
        mission_id: str,
        task_id: str,
        step_id: str,
        plan_revision: int,
        step_attempt: int,
        planned_action_digest: str,
        verification_contract_sha256: str | None,
        outcome_tool: str,
        action_tool: str,
        verification: VerificationResult,
        inspector: str = "state_verifier",
    ) -> TrustedInspectorEvidence:
        return TrustedInspectorEvidence(
            _issuer=self._inspector_issuer,
            protocol="jarvis.inspector-evidence.v1",
            mission_id=mission_id,
            task_id=task_id,
            step_id=step_id,
            plan_revision=plan_revision,
            step_attempt=step_attempt,
            environment_digest=self._environment.digest,
            planned_action_digest=planned_action_digest,
            verification_contract_sha256=verification_contract_sha256,
            outcome_tool=outcome_tool,
            action_tool=action_tool,
            action_id=verification.action_id,
            action_kind=verification.action_kind,
            inspector=inspector,
            ok=verification.ok,
            status=verification.status.value,
            summary=verification.summary[:1000],
            evidence=tuple(item.to_dict() for item in verification.evidence),
        )

    def _verification_digest(
        self,
        evidence: TrustedInspectorEvidence | None,
        *,
        mission_id: str,
        task_id: str,
        step_id: str,
        planner: AdaptiveDAGPlanner,
        result: ToolRunResponse,
    ) -> dict[str, Any]:
        current = _step_snapshot(planner, step_id)
        snapshot = planner.snapshot()
        accepted = bool(
            isinstance(evidence, TrustedInspectorEvidence)
            and evidence._issuer is self._inspector_issuer
            and evidence.protocol == "jarvis.inspector-evidence.v1"
            and evidence.mission_id == mission_id
            and evidence.task_id == task_id
            and evidence.step_id == step_id
            and evidence.plan_revision == snapshot.revision
            and evidence.step_attempt == current.attempts
            and evidence.environment_digest == self._environment.digest
            and evidence.planned_action_digest
            == _planned_action_digest(current.spec, current.bound_action)
            and evidence.verification_contract_sha256
            == _contract_digest(current.verification_contract)
            and evidence.outcome_tool == result.tool
            and bool(evidence.action_tool)
            and bool(evidence.action_id)
            and bool(evidence.action_kind)
            and bool(evidence.evidence)
        )
        if not accepted or evidence is None:
            return {
                "available": False,
                "ok": False,
                "status": "rejected",
                "summary": "Trusted plan-bound inspector evidence was not provided.",
            }
        return {
            "available": True,
            "ok": evidence.ok,
            "status": evidence.status,
            "summary": evidence.summary,
            "inspector": evidence.inspector,
            "action": {
                "tool": evidence.action_tool,
                "action_id": evidence.action_id,
                "action_kind": evidence.action_kind,
            },
            "binding": {
                "mission_id": evidence.mission_id,
                "task_id": evidence.task_id,
                "step_id": evidence.step_id,
                "plan_revision": evidence.plan_revision,
                "step_attempt": evidence.step_attempt,
                "environment_digest": evidence.environment_digest,
                "planned_action_digest": evidence.planned_action_digest,
                "verification_contract_sha256": (evidence.verification_contract_sha256),
            },
            "evidence": list(evidence.evidence),
        }

    def _fail_closed_unplanned_mission(
        self,
        mission: dict[str, Any],
        reason: str,
    ) -> None:
        mission_id = str(mission.get("id") or "")
        tasks = mission.get("tasks") if isinstance(mission.get("tasks"), list) else []
        for task in tasks:
            if task.get("status") in {"done", "skipped"}:
                continue
            self.storage.update_mission_task(
                str(task.get("id") or ""),
                mission_id=mission_id,
                status="blocked",
                notes=f"Executive fail-closed quarantine: {reason}"[:20000],
            )

    def snapshot(self, mission_id: str) -> dict[str, Any] | None:
        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                return None
            planner, record = loaded
            return self._record(
                mission_id,
                planner,
                dict(record["task_map"]),
                list(record.get("playbooks") or []),
            )

    def claim_ready_task(self, mission_id: str) -> ExecutiveStepClaim | None:
        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                return None
            planner, record = loaded
            task_map = dict(record["task_map"])
            if planner.snapshot().environment.digest != self._environment.digest:
                planner, task_map = self._adapt_environment(mission_id, planner, task_map)
                self._persist(
                    mission_id,
                    self._record(
                        mission_id,
                        planner,
                        task_map,
                        list(record.get("playbooks") or []),
                    ),
                )
            tasks = {str(item["id"]): item for item in self.storage.list_mission_tasks(mission_id)}
            for step_id in planner.ready_step_ids(self._environment):
                task_id = str(task_map.get(step_id) or "")
                task = tasks.get(task_id)
                if task is None or task.get("status") != "pending":
                    continue
                claimed = self.storage.claim_mission_task(mission_id, task_id)
                if claimed is None:
                    continue
                try:
                    planner.start_step(step_id, environment=self._environment)
                except Exception:
                    self.storage.update_mission_task(
                        task_id, mission_id=mission_id, status="pending"
                    )
                    raise
                saved = self._record(
                    mission_id,
                    planner,
                    task_map,
                    list(record.get("playbooks") or []),
                )
                self._persist(mission_id, saved)
                return ExecutiveStepClaim(step_id, claimed, saved["planner"])
            return None

    def record_step(
        self,
        mission_id: str,
        task_id: str,
        result: ToolRunResponse,
        *,
        inspector_evidence: TrustedInspectorEvidence | None = None,
    ) -> ExecutiveStepOutcome:
        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                raise KeyError(f"executive plan is missing for mission {mission_id}")
            planner, record = loaded
            task_map = dict(record["task_map"])
            step_id = next(
                (step for step, mapped in task_map.items() if mapped == task_id),
                None,
            )
            if step_id is None:
                raise KeyError(f"mission task {task_id} is not mapped to the DAG")
            current_step = _step_snapshot(planner, step_id)
            if current_step.status is StepStatus.FAILED and result.ok:
                planner.retry_step(step_id)
                planner.start_step(step_id, environment=self._environment)
                current_step = _step_snapshot(planner, step_id)
            verification = self._verification_digest(
                inspector_evidence,
                mission_id=mission_id,
                task_id=task_id,
                step_id=step_id,
                planner=planner,
                result=result,
            )
            verified = bool(
                result.ok and verification.get("available") and verification.get("ok") is True
            )
            if result.ok:
                planner.begin_verification(
                    step_id,
                    action_evidence={
                        "tool": result.tool,
                        "ok": result.ok,
                        "summary": result.summary[:2000],
                        "state_verification": verification,
                    },
                )
                criterion = _step_snapshot(planner, step_id).spec.criteria[0]
                planner.record_verification(
                    step_id,
                    results=(
                        AssertionResult(
                            assertion_id=criterion.assertion_id,
                            inspector=criterion.inspector,
                            passed=verified,
                            evidence={
                                "tool_result_ok": True,
                                "state_verification": verification,
                            },
                        ),
                    ),
                )
                if verified and planner.status is GoalStatus.VERIFYING:
                    planner.record_goal_verification(
                        results=(
                            AssertionResult(
                                assertion_id="goal.result",
                                inspector="mission.goal.result",
                                passed=True,
                                evidence={
                                    "all_steps_verified": True,
                                    "step_count": len(planner.snapshot().steps),
                                },
                            ),
                        )
                    )
                if verified:
                    adapted = False
                    added: tuple[str, ...] = ()
                else:
                    planner, task_map, added = self._adapt_failure(
                        mission_id,
                        planner,
                        task_map,
                        step_id=step_id,
                        reason="independent step verification was absent or failed",
                    )
                    adapted = bool(added)
            else:
                if current_step.status in {StepStatus.RUNNING, StepStatus.VERIFYING}:
                    planner.fail_step(step_id, reason=result.summary or "step failed")
                failure_reason = result.summary or "unexpected tool result"
                if _requires_reconcile_only_recovery(result):
                    failure_reason = (
                        "[reconcile-only] transaction rollback was incomplete or its "
                        f"outcome is ambiguous; never replay the original action: {failure_reason}"
                    )
                planner, task_map, added = self._adapt_failure(
                    mission_id,
                    planner,
                    task_map,
                    step_id=step_id,
                    reason=failure_reason,
                )
                adapted = bool(added)
            saved = self._record(
                mission_id,
                planner,
                task_map,
                list(record.get("playbooks") or []),
            )
            self._persist(mission_id, saved)
            return ExecutiveStepOutcome(
                step_id=step_id,
                verified=verified,
                adapted=adapted,
                planner=saved["planner"],
                added_task_ids=added,
            )

    def terminate_mission(self, mission_id: str, *, reason: str) -> dict[str, Any]:
        """Force an unrecoverable plan into an explicit terminal state."""

        with self._lock:
            loaded = self._load(mission_id)
            if loaded is None:
                raise KeyError(f"executive plan is missing for mission {mission_id}")
            planner, record = loaded
            if planner.status not in {
                GoalStatus.SUCCEEDED,
                GoalStatus.FAILED,
                GoalStatus.CANCELLED,
            }:
                planner.cancel(reason[:4000])
            task_map = {str(key): str(value) for key, value in record["task_map"].items()}
            tasks = {str(item["id"]): item for item in self.storage.list_mission_tasks(mission_id)}
            for task_id in task_map.values():
                task = tasks.get(task_id)
                if task is None or task.get("status") in {"done", "skipped"}:
                    continue
                self.storage.update_mission_task(
                    task_id,
                    mission_id=mission_id,
                    status="blocked",
                    notes=f"Executive mission terminated: {reason}"[:20000],
                )
            saved = self._record(
                mission_id,
                planner,
                task_map,
                list(record.get("playbooks") or []),
            )
            self._persist(mission_id, saved)
            return saved

    def _adapt_failure(
        self,
        mission_id: str,
        planner: AdaptiveDAGPlanner,
        task_map: dict[str, str],
        *,
        step_id: str,
        reason: str,
    ) -> tuple[AdaptiveDAGPlanner, dict[str, str], tuple[str, ...]]:
        snapshot = planner.snapshot()
        if snapshot.revision >= snapshot.limits.max_revisions:
            planner.cancel(
                "graph revision budget exhausted after failed verification: " + reason[:3000]
            )
            tasks = {str(item["id"]): item for item in self.storage.list_mission_tasks(mission_id)}
            for task_id in task_map.values():
                task = tasks.get(str(task_id))
                if task is None or task.get("status") in {"done", "skipped"}:
                    continue
                self.storage.update_mission_task(
                    str(task_id),
                    mission_id=mission_id,
                    status="blocked",
                    notes=(
                        "Executive plan terminated after exhausting its revision budget: " + reason
                    )[:20000],
                )
            return planner, task_map, ()
        original_step = _step_snapshot(planner, step_id)
        original = original_step.spec
        reconcile_only = reason.startswith("[reconcile-only]")
        reconciliation_action = (
            ActionCall(
                tool="execution.verify",
                arguments={
                    "source_tool": original_step.bound_action.tool,
                    "arguments": original_step.bound_action.arguments,
                },
            )
            if reconcile_only
            and original_step.bound_action is not None
            and original_step.bound_action.tool in _EXECUTIVE_MUTATION_TOOLS
            else None
        )
        next_revision = snapshot.revision + 1
        diagnose_id = f"diagnose.r{next_revision}"
        recover_id = f"recover.r{next_revision}"
        diagnostic_task = self.storage.add_mission_task(
            mission_id,
            title=f"Diagnose unexpected result: {original.title}"[:500],
        )
        recovery_task = self.storage.add_mission_task(
            mission_id,
            title=(
                f"Inspect state and reconcile without replay: {original.title}"
                if reconcile_only
                else f"Apply revised approach and re-verify: {original.title}"
            )[:500],
        )
        criterion = _criterion
        diagnostic = StepSpec(
            step_id=diagnose_id,
            title=diagnostic_task["title"],
            objective=f"Determine why the prior step failed: {reason}"[:4000],
            action=ActionCall(tool="mission.execute_step", arguments={"kind": "diagnosis"}),
            dependencies=original.dependencies,
            criteria=(criterion(diagnose_id),),
            preconditions=_profile_preconditions(self._environment),
            evidence_policy="observation",
        )
        recovery = StepSpec(
            step_id=recover_id,
            title=recovery_task["title"],
            objective=(
                (
                    "Inspect the current state after an ambiguous committed action, "
                    "reconcile its outcome, and independently verify it without repeating "
                    f"the original side effect. Required state: {original.objective}. "
                    f"Failure context: {reason}"
                )
                if reconcile_only
                else f"Use diagnostic evidence to recover and independently verify: {reason}"
            )[:4000],
            action=(
                reconciliation_action
                or ActionCall(
                    tool="mission.execute_step",
                    arguments={
                        "kind": "reconciliation" if reconcile_only else "recovery",
                        "replay_original_action": False if reconcile_only else None,
                    },
                )
            ),
            dependencies=(diagnose_id,),
            criteria=(
                criterion(
                    recover_id,
                    objective=original.objective,
                    description=original.criteria[0].description,
                ),
            ),
            preconditions=_profile_preconditions(self._environment),
            evidence_policy=original.evidence_policy,
        )
        replacements: list[StepSpec] = []
        for item in snapshot.steps:
            spec = item.spec
            if spec.step_id == step_id or step_id not in spec.dependencies:
                continue
            dependencies = tuple(
                sorted(recover_id if dep == step_id else dep for dep in spec.dependencies)
            )
            replacements.append(spec.model_copy(update={"dependencies": dependencies}, deep=True))
        replacements_by_id = {item.step_id: item for item in replacements}
        candidate_specs = tuple(
            replacements_by_id.get(item.spec.step_id, item.spec)
            for item in snapshot.steps
            if item.spec.step_id != step_id
        ) + (diagnostic, recovery)
        _validate_goal_evidence_coverage(snapshot.goal.objective, candidate_specs)
        try:
            planner.apply_revision(
                PlanRevision(
                    revision_id=f"failure.r{next_revision}",
                    goal_id=mission_id,
                    base_revision=snapshot.revision,
                    reason=reason[:4000],
                    environment=self._environment,
                    add_steps=(diagnostic, recovery),
                    replace_steps=tuple(replacements),
                    remove_step_ids=(step_id,),
                )
            )
        except Exception:
            self.storage.update_mission_task(
                diagnostic_task["id"], mission_id=mission_id, status="skipped"
            )
            self.storage.update_mission_task(
                recovery_task["id"], mission_id=mission_id, status="skipped"
            )
            raise
        previous_task = task_map.pop(step_id, None)
        if previous_task:
            self.storage.update_mission_task(
                previous_task,
                mission_id=mission_id,
                status="skipped",
                notes=f"DAG branch replaced after failure: {reason}"[:20000],
            )
        task_map[diagnose_id] = str(diagnostic_task["id"])
        task_map[recover_id] = str(recovery_task["id"])
        return planner, task_map, (str(diagnostic_task["id"]), str(recovery_task["id"]))

    def _adapt_environment(
        self,
        mission_id: str,
        planner: AdaptiveDAGPlanner,
        task_map: dict[str, str],
    ) -> tuple[AdaptiveDAGPlanner, dict[str, str]]:
        snapshot = planner.snapshot()
        if snapshot.status not in {GoalStatus.READY, GoalStatus.RUNNING}:
            return planner, task_map
        active_step_ids = tuple(
            item.spec.step_id
            for item in snapshot.steps
            if item.status in {StepStatus.RUNNING, StepStatus.VERIFYING}
        )
        for active_step_id in active_step_ids:
            current = next(
                (item for item in planner.snapshot().steps if item.spec.step_id == active_step_id),
                None,
            )
            if current is None or current.status not in {
                StepStatus.RUNNING,
                StepStatus.VERIFYING,
            }:
                continue
            task_id = str(task_map.get(active_step_id) or "")
            self._invalidate_step_approvals(
                mission_id,
                task_id,
                active_step_id,
                reason="host fingerprint changed before approved execution",
            )
            planner.fail_step(
                active_step_id,
                reason="host fingerprint changed while the step was active",
            )
            planner, task_map, _added = self._adapt_failure(
                mission_id,
                planner,
                task_map,
                step_id=active_step_id,
                reason=(
                    "[reconcile-only] host fingerprint changed while an action was pending; "
                    "invalidate stale approvals and inspect state without replay"
                ),
            )
            if planner.status in {
                GoalStatus.SUCCEEDED,
                GoalStatus.FAILED,
                GoalStatus.CANCELLED,
            }:
                return planner, task_map
        snapshot = planner.snapshot()
        # Failure adaptation already installs the current environment on its
        # revision.  In that case a second revalidation revision is redundant.
        if snapshot.environment.digest == self._environment.digest:
            return planner, task_map
        revision_number = snapshot.revision + 1
        task = self.storage.add_mission_task(
            mission_id,
            title="Revalidate changed host environment before continuing",
        )
        step_id = f"environment.r{revision_number}"
        succeeded = tuple(
            item.spec.step_id for item in snapshot.steps if item.status is StepStatus.SUCCEEDED
        )
        environment_step = StepSpec(
            step_id=step_id,
            title=task["title"],
            objective="Refresh assumptions and tool availability for the current host profile.",
            action=ActionCall(tool="environment.profile", arguments={}),
            dependencies=tuple(sorted(succeeded)),
            criteria=(_criterion(step_id),),
            evidence_policy="observation",
        )
        replacements = []
        for item in snapshot.steps:
            if item.status is StepStatus.SUCCEEDED:
                continue
            dependencies = tuple(sorted({*item.spec.dependencies, step_id}))
            replacements.append(
                item.spec.model_copy(
                    update={
                        "dependencies": dependencies,
                        "preconditions": _profile_preconditions(self._environment),
                    },
                    deep=True,
                )
            )
        replacements_by_id = {item.step_id: item for item in replacements}
        candidate_specs = tuple(
            replacements_by_id.get(item.spec.step_id, item.spec) for item in snapshot.steps
        ) + (environment_step,)
        _validate_goal_evidence_coverage(snapshot.goal.objective, candidate_specs)
        try:
            planner.apply_revision(
                PlanRevision(
                    revision_id=f"environment.r{revision_number}",
                    goal_id=mission_id,
                    base_revision=snapshot.revision,
                    reason="cold-start host fingerprint changed",
                    environment=self._environment,
                    add_steps=(environment_step,),
                    replace_steps=tuple(replacements),
                )
            )
        except Exception:
            self.storage.update_mission_task(task["id"], mission_id=mission_id, status="skipped")
            raise
        task_map[step_id] = str(task["id"])
        return planner, task_map

    def _invalidate_step_approvals(
        self,
        mission_id: str,
        task_id: str,
        step_id: str,
        *,
        reason: str,
    ) -> None:
        if not task_id:
            return
        for status in ("pending", "approved"):
            for approval in self.storage.list_approvals(limit=10_000, status=status):
                payload = approval.get("payload")
                if not isinstance(payload, dict):
                    continue
                claim = payload.get("executive_claim")
                if (
                    payload.get("mission_id") != mission_id
                    or payload.get("task_id") != task_id
                    or not isinstance(claim, dict)
                    or claim.get("protocol") != "jarvis.executive-approval.v1"
                    or claim.get("step_id") != step_id
                ):
                    continue
                try:
                    self.storage.invalidate_mission_approval(
                        str(approval["id"]),
                        reason=reason,
                    )
                except ValueError:
                    # A concurrent claim won the state transition.  Its strict
                    # environment-bound claim validation remains the final gate.
                    continue

    def _lookup_playbooks(self, goal: str) -> list[dict[str, Any]]:
        if self.playbooks is None:
            return []
        try:
            return [
                {
                    "id": item.id,
                    "symptom": item.symptom[:1000],
                    "solution": item.solution[:1500],
                    "verification": item.verification[:1000],
                    "confidence": item.confidence,
                }
                for item in self.playbooks.lookup(goal, limit=5)
            ]
        except (OSError, RuntimeError, TypeError, ValueError):
            return []

    def _load(self, mission_id: str) -> tuple[AdaptiveDAGPlanner, dict[str, Any]] | None:
        value = self.storage.get_runtime_value(_KEY_PREFIX + mission_id, None)
        if not isinstance(value, dict) or value.get("protocol") != EXECUTIVE_PROTOCOL:
            return None
        planner_value = value.get("planner")
        task_map = value.get("task_map")
        if not isinstance(planner_value, dict) or not isinstance(task_map, dict):
            raise ValueError("persisted executive plan is malformed")
        planner = AdaptiveDAGPlanner.restore(
            PlannerSnapshot.model_validate(planner_value),
            recover_inflight=False,
        )
        return planner, value

    def _recover_interrupted_plans(self) -> None:
        """Reconcile planner snapshots and mission rows after a primary cold start.

        Planner state and mission rows are intentionally stored independently for
        queryability, so a process can stop between their two durable writes.  The
        recovery pass handles both write orders and preserves an active step only
        when a matching, environment-bound approval can still be resumed.
        """

        with self._lock:
            self._recover_unplanned_missions()
            for item in self.storage.list_runtime_values(prefix=_KEY_PREFIX):
                key = str(item.get("key") or "")
                key_mission_id = key[len(_KEY_PREFIX) :] if key.startswith(_KEY_PREFIX) else ""
                try:
                    value = item.get("value")
                    if not isinstance(value, dict) or value.get("protocol") != EXECUTIVE_PROTOCOL:
                        raise ValueError("unknown or malformed executive record protocol")
                    mission_id = str(value.get("mission_id") or "")
                    planner_value = value.get("planner")
                    task_map_value = value.get("task_map")
                    if (
                        not mission_id
                        or mission_id != key_mission_id
                        or not isinstance(planner_value, dict)
                        or not isinstance(task_map_value, dict)
                        or any(
                            not isinstance(step_id, str)
                            or not step_id
                            or not isinstance(task_id, str)
                            or not task_id
                            for step_id, task_id in task_map_value.items()
                        )
                    ):
                        raise ValueError("persisted executive plan is malformed")
                    source = PlannerSnapshot.model_validate(planner_value)
                    # Restore once without recovery to validate cross-field graph,
                    # attempt, assertion, and revision invariants before any
                    # durable mission row is touched.
                    AdaptiveDAGPlanner.restore(source, recover_inflight=False)
                    task_map = dict(task_map_value)
                    step_ids = {step.spec.step_id for step in source.steps}
                    if set(task_map) != step_ids or len(set(task_map.values())) != len(task_map):
                        raise ValueError("persisted executive task map is inconsistent")
                except (RuntimeError, TypeError, ValueError) as exc:
                    self._quarantine_malformed_plan(
                        key_mission_id,
                        error_type=type(exc).__name__,
                    )
                    continue
                tasks = {
                    str(task["id"]): task for task in self.storage.list_mission_tasks(mission_id)
                }
                if set(task_map.values()) - set(tasks):
                    self._quarantine_malformed_plan(
                        mission_id,
                        error_type="MissingMissionTask",
                    )
                    continue
                mapped_task_ids = set(task_map.values())
                for task_id, task in tuple(tasks.items()):
                    if task_id in mapped_task_ids or task.get("status") in {
                        "done",
                        "skipped",
                    }:
                        continue
                    # A DAG adaptation creates rows before its revised snapshot is
                    # committed.  Rows absent from the durable task map therefore
                    # belong to an interrupted revision and must never enter a FIFO
                    # execution path after restart.
                    updated = self.storage.update_mission_task(
                        task_id,
                        mission_id=mission_id,
                        status=("blocked" if task.get("status") == "running" else "skipped"),
                        notes=(
                            "Cold-start executive quarantine: mission task is not mapped "
                            "by the last durable DAG revision; it will not be executed."
                        ),
                    )
                    if updated is not None:
                        tasks[task_id] = updated
                # Close the two deterministic crash windows before recovering
                # active execution.  A verified planner step is authoritative;
                # a mission row claimed before planner.start_step is safe to
                # release because the action was not yet made executable.
                for step in source.steps:
                    task_id = task_map.get(step.spec.step_id, "")
                    task = tasks.get(task_id)
                    if task is None:
                        continue
                    task_status = str(task.get("status") or "")
                    next_status: str | None = None
                    note: str | None = None
                    if step.status is StepStatus.SUCCEEDED and task_status not in {
                        "done",
                        "skipped",
                    }:
                        next_status = "done"
                        note = (
                            "Cold-start reconciliation: the independently verified DAG "
                            "step committed before its mission row."
                        )
                    elif task_status == "running" and step.status is StepStatus.PENDING:
                        next_status = "pending"
                        note = (
                            "Cold-start reconciliation: the task claim committed before "
                            "the DAG step started; no action is being replayed."
                        )
                    elif task_status == "running" and step.status is StepStatus.BLOCKED:
                        next_status = "blocked"
                        note = "Cold-start reconciliation: planner preconditions remain blocked."
                    elif task_status == "running" and step.status in {
                        StepStatus.FAILED,
                        StepStatus.CANCELLED,
                    }:
                        next_status = "blocked"
                        note = (
                            "Cold-start reconciliation: the DAG step is terminal and requires "
                            "inspection."
                        )
                    if next_status is not None:
                        updated = self.storage.update_mission_task(
                            task_id,
                            mission_id=mission_id,
                            status=next_status,
                            notes=note,
                        )
                        if updated is not None:
                            tasks[task_id] = updated
                active_step_ids = tuple(
                    step.spec.step_id
                    for step in source.steps
                    if step.status in {StepStatus.RUNNING, StepStatus.VERIFYING}
                )
                if not active_step_ids:
                    continue
                # Approval creation commits before the agent changes the mission
                # row from ``running`` to ``blocked``.  A power loss in that
                # narrow window leaves an otherwise resumable capability whose
                # execution gate rejects the still-running task.  The exact,
                # plan-bound approval is durable proof that execution reached the
                # gate, so finish that half-committed transition without replaying
                # the action.
                for step_id in active_step_ids:
                    task_id = task_map.get(step_id, "")
                    task = tasks.get(task_id)
                    if (
                        task is None
                        or task.get("status") != "running"
                        or not self._has_resumable_approval(
                            mission_id,
                            task_id,
                            step_id,
                            source,
                        )
                    ):
                        continue
                    updated = self.storage.update_mission_task(
                        task_id,
                        mission_id=mission_id,
                        status="blocked",
                        notes=(
                            "Cold-start reconciliation: a durable, plan-bound approval "
                            "was created before the task entered its blocked state."
                        ),
                    )
                    if updated is not None:
                        tasks[task_id] = updated
                interrupted = tuple(
                    step_id
                    for step_id in active_step_ids
                    if not self._has_resumable_approval(
                        mission_id,
                        task_map.get(step_id, ""),
                        step_id,
                        source,
                    )
                )
                if not interrupted:
                    continue
                planner = AdaptiveDAGPlanner.restore(
                    source,
                    recover_inflight=True,
                    recover_step_ids=frozenset(interrupted),
                )
                for step_id in interrupted:
                    task_id = task_map.get(step_id, "")
                    current = next(
                        (step for step in planner.snapshot().steps if step.spec.step_id == step_id),
                        None,
                    )
                    if current is None or current.status is not StepStatus.FAILED:
                        continue
                    self._invalidate_step_approvals(
                        mission_id,
                        task_id,
                        step_id,
                        reason="approval execution was interrupted or is no longer resumable",
                    )
                    planner, task_map, _added = self._adapt_failure(
                        mission_id,
                        planner,
                        task_map,
                        step_id=step_id,
                        reason=(
                            "[reconcile-only] cold start found an active DAG step without "
                            "a resumable approval; its side-effect outcome is ambiguous, so "
                            "inspect durable state without replay"
                        ),
                    )
                    if planner.status in {
                        GoalStatus.SUCCEEDED,
                        GoalStatus.FAILED,
                        GoalStatus.CANCELLED,
                    }:
                        break
                self._persist(
                    mission_id,
                    self._record(
                        mission_id,
                        planner,
                        task_map,
                        list(value.get("playbooks") or []),
                    ),
                )

    def _quarantine_malformed_plan(
        self,
        mission_id: str,
        *,
        error_type: str,
    ) -> None:
        """Fail one damaged mission closed without preventing primary startup."""

        mission = self.storage.get_mission(mission_id) if mission_id else None
        if mission is not None:
            self._fail_closed_unplanned_mission(
                mission,
                "persisted executive plan failed schema and integrity validation",
            )
        self.storage.add_event(
            kind="executive.plan.quarantine",
            title="Malformed executive plan quarantined",
            level="error",
            payload={
                "mission_id": mission_id or None,
                "error_type": str(error_type)[:160],
            },
        )

    def _recover_unplanned_missions(self) -> None:
        """Backfill only pristine create-window missions; quarantine every other case."""

        for mission in self.storage.list_missions(limit=1_000_000):
            mission_id = str(mission.get("id") or "")
            if not mission_id or mission.get("status") == "done":
                continue
            raw = self.storage.get_runtime_value(_KEY_PREFIX + mission_id, _MISSING)
            if raw is not _MISSING:
                continue
            try:
                self.ensure_for_mission(mission)
            except (KeyError, RuntimeError, TypeError, ValueError):
                # ensure_for_mission already put every unfinished row behind the
                # fail-closed barrier; one damaged mission must not stop recovery
                # of unrelated durable plans.
                continue

    def _has_resumable_approval(
        self,
        mission_id: str,
        task_id: str,
        step_id: str,
        snapshot: PlannerSnapshot,
    ) -> bool:
        if not task_id or snapshot.environment.digest != self._environment.digest:
            return False
        step = next(
            (item for item in snapshot.steps if item.spec.step_id == step_id),
            None,
        )
        if step is None:
            return False
        for status in ("pending", "approved"):
            for approval in self.storage.list_approvals(limit=10_000, status=status):
                payload = approval.get("payload")
                if not isinstance(payload, dict):
                    continue
                claim = payload.get("executive_claim")
                arguments = payload.get("arguments")
                tool = str(payload.get("tool") or "")
                expected_contract = (
                    step.verification_contract.model_dump(mode="json")
                    if step.verification_contract is not None
                    else None
                )
                exact_contract = (
                    _verification_contract(step.spec, tool, arguments)
                    if tool in _EXECUTIVE_MUTATION_TOOLS
                    and isinstance(arguments, dict)
                    else None
                )
                if (
                    approval.get("requested_action") == "tool.run"
                    and tool in _EXECUTIVE_MUTATION_TOOLS
                    and payload.get("mission_id") == mission_id
                    and payload.get("task_id") == task_id
                    and isinstance(claim, dict)
                    and claim.get("protocol") == "jarvis.executive-approval.v1"
                    and claim.get("mission_id") == mission_id
                    and claim.get("task_id") == task_id
                    and claim.get("step_id") == step_id
                    and claim.get("plan_revision") == snapshot.revision
                    and claim.get("step_attempt") == step.attempts
                    and claim.get("environment_digest") == snapshot.environment.digest
                    and step.bound_action is not None
                    and expected_contract is not None
                    and claim.get("verification_contract") == expected_contract
                    and exact_contract is not None
                    and exact_contract == step.verification_contract
                    and step.bound_action
                    == ActionCall(
                        tool=tool,
                        arguments=arguments,
                        destructive=True,
                    )
                ):
                    return True
        return False

    def _record(
        self,
        mission_id: str,
        planner: AdaptiveDAGPlanner,
        task_map: dict[str, str],
        playbooks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "protocol": EXECUTIVE_PROTOCOL,
            "mission_id": mission_id,
            "planner": planner.snapshot().model_dump(mode="json"),
            "task_map": dict(sorted(task_map.items())),
            "playbooks": playbooks[:5],
        }

    def _persist(self, mission_id: str, record: dict[str, Any]) -> None:
        self.storage.set_runtime_value(_KEY_PREFIX + mission_id, record)


def validate_mission_decomposition(value: Any) -> MissionDecomposition:
    """Validate an untrusted bounded task proposal before it reaches storage.

    The proposal deliberately cannot choose tools or action payloads.  It only
    supplies task-specific objectives, dependencies, and assertion text; the
    coordinator materialises the canonical execution action and inspector.
    """

    if isinstance(value, MissionDecomposition):
        value = {
            "protocol": value.protocol,
            "steps": [
                {
                    "step_id": item.step_id,
                    "title": item.title,
                    "objective": item.objective,
                    "dependencies": list(item.dependencies),
                    "assertion": item.assertion,
                }
                for item in value.steps
            ],
            "rationale": value.rationale,
        }
    if not isinstance(value, dict) or set(value) != {
        "protocol",
        "steps",
        "rationale",
    }:
        raise ValueError("mission decomposition must use the exact versioned schema")
    if value.get("protocol") != MISSION_DECOMPOSITION_PROTOCOL:
        raise ValueError("unsupported mission decomposition protocol")
    raw_steps = value.get("steps")
    rationale = value.get("rationale")
    if not isinstance(raw_steps, list) or not 2 <= len(raw_steps) <= 24:
        raise ValueError("mission decomposition requires 2..24 bounded steps")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000:
        raise ValueError("mission decomposition rationale must be non-empty and bounded")
    parsed: list[MissionDecompositionStep] = []
    for raw in raw_steps:
        if not isinstance(raw, dict) or set(raw) != {
            "step_id",
            "title",
            "objective",
            "dependencies",
            "assertion",
        }:
            raise ValueError("mission decomposition step has unknown or missing fields")
        step_id = raw.get("step_id")
        title = raw.get("title")
        objective = raw.get("objective")
        dependencies = raw.get("dependencies")
        assertion = raw.get("assertion")
        if not isinstance(step_id, str) or not _STEP_ID_PATTERN.fullmatch(step_id):
            raise ValueError("mission decomposition step_id is invalid")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError("mission decomposition title must be non-empty and bounded")
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
            raise ValueError("mission decomposition objective must be non-empty and bounded")
        if not isinstance(assertion, str) or not assertion.strip() or len(assertion) > 1000:
            raise ValueError("mission decomposition assertion must be non-empty and bounded")
        if (
            not isinstance(dependencies, list)
            or len(dependencies) > 23
            or any(
                not isinstance(item, str) or not _STEP_ID_PATTERN.fullmatch(item)
                for item in dependencies
            )
            or len(set(dependencies)) != len(dependencies)
            or step_id in dependencies
        ):
            raise ValueError("mission decomposition dependencies are invalid")
        parsed.append(
            MissionDecompositionStep(
                step_id=step_id,
                title=title.strip(),
                objective=objective.strip(),
                dependencies=tuple(sorted(dependencies)),
                assertion=assertion.strip(),
                evidence_policy=_evidence_policy(title, objective, assertion),
            )
        )
    ids = {item.step_id for item in parsed}
    if len(ids) != len(parsed):
        raise ValueError("mission decomposition step ids must be unique")
    if any(dependency not in ids for item in parsed for dependency in item.dependencies):
        raise ValueError("mission decomposition dependency does not exist")
    _assert_acyclic_decomposition(tuple(parsed))
    return MissionDecomposition(
        protocol=MISSION_DECOMPOSITION_PROTOCOL,
        steps=tuple(parsed),
        rationale=rationale.strip(),
    )


def validate_mission_goal_coverage(
    goal: str,
    decomposition: MissionDecomposition,
) -> MissionDecomposition:
    """Reject model DAGs that weaken the authoritative goal evidence class."""

    required_policy = _goal_required_policy(goal)
    if required_policy is None:
        return decomposition
    matching_steps = tuple(
        item for item in decomposition.steps if item.evidence_policy == required_policy
    )
    if not matching_steps:
        raise ValueError(
            f"{required_policy} mission goal requires at least one independently "
            f"verified {required_policy} step"
        )
    goal_terms = _semantic_terms(goal)
    required = min(2, len(goal_terms))
    if required == 0 or not any(
        len(goal_terms & _semantic_terms(" ".join((item.title, item.objective, item.assertion))))
        >= required
        for item in matching_steps
    ):
        raise ValueError(
            f"{required_policy} mission goal is not covered by a goal-bound "
            f"{required_policy} assertion"
        )
    return decomposition


def _assert_acyclic_decomposition(steps: tuple[MissionDecompositionStep, ...]) -> None:
    dependencies = {item.step_id: set(item.dependencies) for item in steps}
    ready = sorted(step_id for step_id, items in dependencies.items() if not items)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for step_id in sorted(dependencies):
            if current not in dependencies[step_id]:
                continue
            dependencies[step_id].remove(current)
            if not dependencies[step_id]:
                ready.append(step_id)
        ready.sort()
    if visited != len(steps):
        raise ValueError("mission decomposition must be acyclic")


def _profile_copy(profile: dict[str, Any]) -> dict[str, Any]:
    return dict(profile)


def _environment_from_profile(profile: dict[str, Any]) -> EnvironmentFingerprint:
    fingerprint = str(profile.get("fingerprint_sha256") or "")
    host = profile.get("host") if isinstance(profile.get("host"), dict) else {}
    return EnvironmentFingerprint.capture(
        {
            "profile_fingerprint": fingerprint,
            "os": host.get("os") or {},
            "architecture": host.get("architecture") or {},
            "accelerators": host.get("accelerators") or {},
            "tools": host.get("tools") or {},
        }
    )


def _profile_preconditions(
    environment: EnvironmentFingerprint,
) -> tuple[PreconditionFingerprint, ...]:
    return (
        PreconditionFingerprint.from_environment(
            name="host_profile",
            environment=environment,
            fact_paths=("/profile_fingerprint",),
            description="The cold-start host profile must match the planned environment.",
        ),
    )


def _criterion(
    step_id: str,
    *,
    objective: str = "",
    description: str | None = None,
) -> AssertionCriterion:
    return AssertionCriterion(
        assertion_id=f"result.{step_id}",
        description=(
            description or "Step outcome is independently verified before downstream work starts."
        ),
        inspector="mission.step.result",
        expected={
            "ok": True,
            "objective_sha256": _json_sha256(
                {"step_id": step_id, "objective": objective or step_id}
            ),
        },
    )


def _initial_specs(
    tasks: list[dict[str, Any]],
    environment: EnvironmentFingerprint,
    *,
    goal: str,
    decomposition: MissionDecomposition | None = None,
) -> tuple[tuple[StepSpec, ...], dict[str, str]]:
    if decomposition is not None:
        if len(decomposition.steps) != len(tasks):
            raise ValueError("mission tasks do not match the decomposition")
        specs = []
        task_map = {}
        for proposal, task in zip(decomposition.steps, tasks, strict=True):
            title = str(task.get("title") or "").strip()
            if title != proposal.title:
                raise ValueError("persisted mission task title differs from decomposition")
            specs.append(
                StepSpec(
                    step_id=proposal.step_id,
                    title=proposal.title,
                    objective=proposal.objective,
                    action=ActionCall(
                        tool="mission.execute_step",
                        arguments={"mission_task_id": str(task.get("id") or "")},
                    ),
                    dependencies=proposal.dependencies,
                    criteria=(
                        _criterion(
                            proposal.step_id,
                            objective=proposal.objective,
                            description=proposal.assertion,
                        ),
                    ),
                    preconditions=_profile_preconditions(environment),
                    evidence_policy=proposal.evidence_policy,
                )
            )
            task_map[proposal.step_id] = str(task.get("id") or "")
        _validate_goal_evidence_coverage(goal, tuple(specs))
        return tuple(specs), task_map

    step_ids = tuple(f"step.{index:03d}" for index in range(1, len(tasks) + 1))
    implementation_index = max(2, len(tasks) - 3)
    middle = tuple(range(3, implementation_index))
    specs: list[StepSpec] = []
    task_map: dict[str, str] = {}
    for index, (step_id, task) in enumerate(zip(step_ids, tasks, strict=True)):
        if index == 0:
            dependencies: tuple[str, ...] = ()
        elif index == 1:
            dependencies = (step_ids[0],)
        elif index == 2:
            dependencies = tuple(step_ids[:2])
        elif index < implementation_index:
            dependencies = (step_ids[2],)
        elif index == implementation_index:
            dependencies = tuple(step_ids[item] for item in middle) or (step_ids[2],)
        else:
            dependencies = (step_ids[index - 1],)
        title = str(task.get("title") or f"Mission step {index + 1}")[:500]
        specs.append(
            StepSpec(
                step_id=step_id,
                title=title,
                objective=title,
                action=ActionCall(
                    tool="mission.execute_step",
                    arguments={"mission_task_id": str(task.get("id") or "")},
                ),
                dependencies=dependencies,
                criteria=(_criterion(step_id, objective=title),),
                preconditions=_profile_preconditions(environment),
                evidence_policy=_evidence_policy(title, title, title),
            )
        )
        task_map[step_id] = str(task.get("id") or "")
    _validate_goal_evidence_coverage(goal, tuple(specs))
    return tuple(specs), task_map


def _step_snapshot(planner: AdaptiveDAGPlanner, step_id: str):
    return next(item for item in planner.snapshot().steps if item.spec.step_id == step_id)


def _inspect_read_only_artifact(
    result: ToolRunResponse,
    arguments: dict[str, Any],
    *,
    spec: StepSpec,
) -> VerificationResult | None:
    if (
        result.tool in {"execution.apply", "mission.brief", "mission.execute_next"}
        or spec.evidence_policy == "state"
    ):
        return None
    try:
        arguments_sha256 = _json_sha256(arguments)
        authoritative_data = _strip_tool_request_echoes(result.tool, result.data)
        output_sha256 = _json_sha256(
            {
                "tool": result.tool,
                "ok": result.ok,
                "summary": result.summary,
                "data": result.data,
            }
        )
        encoded_size = len(
            json.dumps(
                result.data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        return None
    expected_terms, matched_terms = _artifact_relevance(
        spec,
        json.dumps(
            {
                "tool": result.tool,
                "summary": result.summary,
                "data": authoritative_data,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )
    substantive = _has_substantive_value(authoritative_data)
    inspection_subject_bound = bool(
        result.tool != "execution.inspect"
        or _inspection_subject_is_plan_bound(spec, result.data, arguments)
    )
    discovered_subjects = (
        _trusted_discovery_subjects(result.tool, authoritative_data, arguments)
        if result.tool in _TRUSTED_SUBJECT_DISCOVERY_TOOLS
        and inspection_subject_bound
        else frozenset()
    )
    discovered_subject_hashes = sorted(
        {_json_sha256(_normalise_subject(value)) for value in discovered_subjects}
    )
    passed = bool(
        result.ok
        and substantive
        and encoded_size <= 4 * 1024 * 1024
        and _has_required_relevance(expected_terms, matched_terms)
        and inspection_subject_bound
    )
    captured_at = datetime.now(UTC).isoformat()
    return VerificationResult(
        ok=passed,
        status=(VerificationStatus.PASSED if passed else VerificationStatus.FAILED),
        action_id=f"readonly:{result.tool}:{arguments_sha256[:24]}",
        action_kind=f"read_only.{result.tool}",
        summary="Independent read-only tool output integrity inspection.",
        evidence=(
            VerificationEvidence(
                source="executive.read_only_artifact",
                assertion=(
                    "direct read-only output is successful, bounded, immutable, and "
                    "relevant to the active step assertion"
                ),
                expected={
                    "ok": True,
                    "max_bytes": 4 * 1024 * 1024,
                    "evidence_policy": spec.evidence_policy,
                    "objective_sha256": _objective_digest(spec),
                    "assertion_sha256": _assertion_digest(spec),
                    "minimum_relevant_terms": _required_relevance_count(expected_terms),
                },
                observed={
                    "tool": result.tool,
                    "arguments_sha256": arguments_sha256,
                    "output_sha256": output_sha256,
                    "encoded_bytes": encoded_size,
                    "data_keys": sorted(str(key) for key in result.data),
                    "matched_terms": sorted(matched_terms),
                    "substantive_output": substantive,
                    "inspection_subject_bound": inspection_subject_bound,
                    "subject_sha256": discovered_subject_hashes,
                },
                passed=passed,
                captured_at=captured_at,
                subject=result.tool,
            ),
        ),
    )


_REQUEST_ECHO_FIELDS = frozenset(
    {
        "arguments",
        "input",
        "limit",
        "mission_id",
        "mode",
        "offset",
        "page",
        "query",
        "request",
        "task_id",
    }
)
_AUTHORITATIVE_SUBJECT_FIELDS = frozenset(
    {
        "destination",
        "destinations",
        "file",
        "files",
        "host",
        "hosts",
        "key",
        "keys",
        "name",
        "names",
        "path",
        "paths",
        "pid",
        "pids",
        "port",
        "ports",
        "session_id",
        "source",
        "sources",
        "subject",
        "target",
        "targets",
        "url",
        "urls",
    }
)
_TRUSTED_SUBJECT_DISCOVERY_TOOLS = frozenset(
    {
        "execution.inspect",
        "files.list",
        "files.search",
        "filesystem.list",
    }
)


def _strip_request_echoes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_request_echoes(item)
            for key, item in value.items()
            if str(key).casefold() not in _REQUEST_ECHO_FIELDS
        }
    if isinstance(value, list):
        return [_strip_request_echoes(item) for item in value]
    return value


def _strip_tool_request_echoes(tool: str, value: Any) -> Any:
    stripped = _strip_request_echoes(value)
    if not isinstance(stripped, dict):
        return stripped
    if tool in {"filesystem.list", "filesystem.read_text"}:
        stripped = {key: item for key, item in stripped.items() if key != "path"}
    return stripped


def _trusted_discovery_subjects(
    tool: str,
    value: Any,
    arguments: dict[str, Any],
) -> frozenset[str]:
    if not isinstance(value, dict):
        return frozenset()
    if tool == "filesystem.list":
        return _authoritative_subject_values(value.get("entries"))
    if tool == "execution.inspect":
        return _intrinsic_inspection_subjects(arguments)
    return _authoritative_subject_values(value)


def _intrinsic_inspection_subjects(arguments: dict[str, Any]) -> frozenset[str]:
    """Return only subjects encoded by the typed read-only action itself.

    Supplemental postconditions may inspect additional paths, sockets, or other
    state.  Those observations prove the current action result but must never
    expand the set of resources a later mutation is authorized to change.
    """

    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        return frozenset()
    try:
        if classify_payload(payload) is not ActionClass.READ_ONLY:
            return frozenset()
        canonical = json.loads(canonical_action_json(payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    action = canonical.get("action")
    if not isinstance(action, dict):
        return frozenset()
    kind = str(action.get("kind") or "")
    if kind in {"fs.stat", "fs.list", "fs.read"}:
        subject = str(action.get("path") or "").strip()
    elif kind in {"network.resolve", "network.tcp_probe"}:
        host = str(action.get("host") or "").strip()
        port = action.get("port")
        subject = f"{host}:{port}" if host and isinstance(port, int) else ""
    elif kind == "registry.get":
        hive = str(action.get("hive") or "").strip()
        key = str(action.get("key") or "").strip()
        name = str(action.get("name") or "").strip()
        subject = f"{hive}\\{key}::{name}" if hive and key and name else ""
    else:
        subject = ""
    return frozenset({subject}) if subject else frozenset()


def _authoritative_subject_values(value: Any, *, field: str = "") -> frozenset[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            values.update(_authoritative_subject_values(item, field=str(key).casefold()))
    elif isinstance(value, list | tuple):
        for item in value:
            values.update(_authoritative_subject_values(item, field=field))
    elif field in _AUTHORITATIVE_SUBJECT_FIELDS and isinstance(value, str | int):
        text = str(value).strip()
        if text:
            values.add(text)
    return frozenset(values)


def _has_substantive_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, list | tuple | set):
        return any(_has_substantive_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_substantive_value(item) for item in value.values())
    return bool(value)


def _inspection_subject_is_plan_bound(
    spec: StepSpec,
    data: dict[str, Any],
    arguments: dict[str, Any],
) -> bool:
    anchors = _literal_inspection_anchors(spec)
    intrinsic_subjects = _intrinsic_inspection_subjects(arguments)
    if not anchors or not intrinsic_subjects:
        return False
    raw_verification = data.get("verification")
    evidence = raw_verification.get("evidence") if isinstance(raw_verification, dict) else None
    verified_subjects = {
        str(item.get("subject") or "")
        for item in evidence or []
        if isinstance(item, dict)
        and item.get("passed") is True
        and str(item.get("subject") or "").strip()
    }
    return bool(
        verified_subjects
        and _anchors_match_subjects(anchors, intrinsic_subjects)
        and all(
            any(
                _anchor_matches_subject(intrinsic, observed)
                for observed in verified_subjects
            )
            for intrinsic in intrinsic_subjects
        )
    )


def _spec_assertion_text(spec: StepSpec) -> str:
    return " ".join(
        (
            spec.title,
            spec.objective,
            *(criterion.description for criterion in spec.criteria),
        )
    )


def _planned_tcp_ports(spec: StepSpec) -> frozenset[int]:
    return frozenset(
        int(match.group(1))
        for match in re.finditer(
            r"\b(?:tcp|port|РїРѕСЂС‚)\s*[:=#]?\s*(\d{2,5})\b",
            _spec_assertion_text(spec),
            flags=re.IGNORECASE,
        )
        if 0 < int(match.group(1)) <= 65535
    )


def _planned_process_ids(spec: StepSpec) -> frozenset[int]:
    return frozenset(
        int(match.group(1))
        for match in re.finditer(
            r"\b(?:pid|process\s+id|РёРґРµРЅС‚РёС„РёРєР°С‚РѕСЂ\s+РїСЂРѕС†РµСЃСЃР°)\s*[:=#]?\s*(\d+)\b",
            _spec_assertion_text(spec),
            flags=re.IGNORECASE,
        )
        if int(match.group(1)) > 0
    )


def _literal_inspection_anchors(spec: StepSpec) -> frozenset[str]:
    text = _spec_assertion_text(spec)
    candidates: set[str] = set()
    candidates.update(re.findall(r"https?://[^\s\]\[(){}<>\"']+", text, flags=re.IGNORECASE))
    candidates.update(value for _start, _end, value in _declared_path_spans(text))
    candidates.update(
        re.findall(
            r"\b[A-Za-z0-9_.-]+\.(?:cfg|conf|csv|docx?|ini|json|md|pdf|toml|txt|xlsx?|yaml|yml)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    candidates.update(
        match.group(1)
        for match in re.finditer(
            r"\b(?:tcp|port|порт)\s*[:=#]?\s*(\d{2,5})\b",
            text,
            flags=re.IGNORECASE,
        )
        if 0 < int(match.group(1)) <= 65535
    )
    candidates.update(
        match.group(1)
        for match in re.finditer(
            r"\b(?:pid|process\s+id|идентификатор\s+процесса)\s*[:=#]?\s*(\d+)\b",
            text,
            flags=re.IGNORECASE,
        )
        if int(match.group(1)) > 0
    )
    return frozenset(
        subject for item in candidates if (subject := _normalise_subject(item.strip(".,;:")))
    )


def _ordered_path_anchors(spec: StepSpec) -> tuple[str, ...]:
    text = _spec_assertion_text(spec)
    ordered: list[str] = []
    for _start, _end, raw in _declared_path_spans(text):
        value = _normalise_subject(raw.strip(".,;:"))
        if value and value not in ordered:
            ordered.append(value)
    return tuple(ordered)


def _normalise_subject(value: str) -> str:
    raw = value.strip().strip("\"'")
    if re.match(r"(?i)^https?://", raw):
        parsed = urlsplit(raw)
        return urlunsplit(
            (
                parsed.scheme.casefold(),
                parsed.netloc.casefold(),
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
    normalized = raw.replace("\\", "/")
    folded_prefix = normalized.casefold()
    if folded_prefix.startswith("//?/unc/"):
        normalized = "//" + normalized[8:]
    elif folded_prefix.startswith("//?/"):
        normalized = normalized[4:]
    if re.match(r"(?i)^[a-z]:/", normalized):
        return re.sub(r"/+", "/", normalized).rstrip("/").casefold()
    if normalized.startswith("//"):
        return "//" + re.sub(r"/+", "/", normalized[2:]).rstrip("/").casefold()
    if normalized.startswith("/"):
        return ("/" + re.sub(r"/+", "/", normalized[1:])).rstrip("/") or "/"
    if normalized.casefold().startswith("hkey_"):
        return normalized.casefold()
    return normalized


def _declared_path_spans(text: str) -> tuple[tuple[int, int, str], ...]:
    """Extract drive, UNC/extended Windows, and POSIX paths with quote support."""

    spans: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []

    def looks_like_path(value: str) -> bool:
        return bool(
            re.match(r"(?i)^[a-z]:[\\/]", value)
            or value.startswith("\\\\")
            or value.startswith("//")
            or value.startswith("/")
        )

    for match in re.finditer(r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)", text):
        value = match.group("value").strip()
        if value and looks_like_path(value):
            spans.append((match.start(), match.end(), value))
            occupied.append((match.start(), match.end()))
    masked = list(text)
    for start, end in occupied:
        masked[start:end] = " " * (end - start)
    remainder = "".join(masked)
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:"
        r"\\\\[^\s\]\[(){}<>\"']+|"
        r"[A-Za-z]:[\\/][^\s\]\[(){}<>\"']+|"
        r"/(?!/)(?:[^\s\]\[(){}<>\"']+/)*[^\s\]\[(){}<>\"']+"
        r")",
    )
    for match in pattern.finditer(remainder):
        value = match.group(0).rstrip(".,;:")
        spans.append((match.start(), match.start() + len(value), value))
    spans.sort(key=lambda item: item[0])
    return tuple(spans)


def _subject_scalar_values(value: Any) -> frozenset[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            values.update(_subject_scalar_values(item))
    elif isinstance(value, list | tuple):
        for item in value:
            values.update(_subject_scalar_values(item))
    elif isinstance(value, str | int):
        text = str(value).strip()
        if text:
            values.add(text)
    return frozenset(values)


def _anchors_match_subjects(
    anchors: frozenset[str],
    subjects: frozenset[str] | set[str],
) -> bool:
    return all(
        any(_anchor_matches_subject(anchor, subject) for subject in subjects) for anchor in anchors
    )


def _anchor_matches_subject(anchor: str, subject: str) -> bool:
    expected = _normalise_subject(anchor)
    raw_subject = subject.strip("\"'")
    observed = _normalise_subject(raw_subject)
    if expected.isdigit():
        return expected in re.findall(r"(?<!\d)\d{1,10}(?!\d)", observed)
    if "/" not in expected and "." in expected and not expected.startswith("http"):
        observed_name = observed.rsplit("/", 1)[-1]
        windows_subject = bool(
            re.match(r"(?i)^(?:[a-z]:[\\/]|\\\\|//\??/)", raw_subject)
        )
        return (
            observed_name.casefold() == expected.casefold()
            if windows_subject
            else observed_name == expected
        )
    return observed == expected


def _combine_read_only_verification(
    strict: VerificationResult,
    scoped: VerificationResult,
) -> VerificationResult:
    passed = bool(strict.ok and scoped.ok)
    return VerificationResult(
        ok=passed,
        status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        action_id=strict.action_id,
        action_kind=strict.action_kind,
        summary="Typed state inspection and DAG assertion scope were both evaluated.",
        evidence=(*strict.evidence, *scoped.evidence),
        error=strict.error or scoped.error,
    )


_SEMANTIC_STOPWORDS = frozenset(
    {
        "about",
        "active",
        "after",
        "against",
        "artifact",
        "assertion",
        "bound",
        "check",
        "checks",
        "complete",
        "concrete",
        "create",
        "current",
        "direct",
        "evidence",
        "expected",
        "file",
        "goal",
        "inspect",
        "mission",
        "output",
        "produce",
        "record",
        "result",
        "retain",
        "satisfies",
        "step",
        "task",
        "that",
        "this",
        "verify",
        "verified",
        "verification",
        "with",
        "активный",
        "артефакт",
        "задача",
        "критерий",
        "миссия",
        "проверить",
        "проверка",
        "результат",
        "создать",
        "текущий",
        "файл",
        "файла",
        "этого",
    }
)

_STATE_ACTION_PATTERN = re.compile(
    r"\b(?:apply|build|configure|delete|deploy|execute|fix|implement|install|migrate|"
    r"modify|patch|provision|remove|restart|start|stop|update|write|"
    r"внедр(?:ить|и)|запуст(?:ить|и)|измен(?:ить|и)|исправ(?:ить|и)|настро(?:ить|й)|"
    r"обнов(?:ить|и)|останов(?:ить|и)|перезапуст(?:ить|и)|разверн(?:уть|и)|"
    r"реализ(?:овать|уй)|удал(?:ить|и)|установ(?:ить|и))\b",
    flags=re.IGNORECASE,
)
_ARTIFACT_GENERATION_PATTERN = re.compile(
    r"\b(?:choose|create|define|design|document|make|map|plan|produce|record|select|summarize|"
    r"synthesi[sz]e|выбрать|зафиксировать|описать|спланировать|сформировать)\b"
    r".{0,96}\b(?:analysis|approach|artifact|boundaries|constraints|decision|design|"
    r"document|map|plan|rationale|report|requirements|specification|strategy|summary|"
    r"анализ|артефакт|границы|карта|ограничения|отчет|план|подход|решение|стратегия)\b",
    flags=re.IGNORECASE | re.DOTALL,
)
_OBSERVATION_PATTERN = re.compile(
    r"\b(?:audit|check|compare|cross-check|diagnose|discover|evaluate|examine|inspect|"
    r"measure|observe|research|scan|test|trace|validate|verify|"
    r"аудит|диагност(?:ика|ировать)|измерить|исследовать|проверить|просканировать|"
    r"сравнить|тестировать)\b",
    flags=re.IGNORECASE,
)
_ARTIFACT_PATTERN = re.compile(
    r"\b(?:analysis|approach|artifact|decision|design|document|map|plan|rationale|"
    r"report|requirements|specification|strategy|summary|"
    r"анализ|артефакт|документ|карта|отчет|план|подход|решение|стратегия)\b",
    flags=re.IGNORECASE,
)
_GOAL_CREATE_STATE_PATTERN = re.compile(
    r"\b(?:create|export|generate|make|render|save|создай|сделай|сгенерируй|сохрани|"
    r"экспортируй)\b.{0,64}\b(?:app|application|artifact|code|config|configuration|"
    r"container|database|directory|docx?|document|file|output|pdf|report|service|"
    r"spreadsheet|system|tool|workbook|xlsx?|баз[ау]|документ|код|конфигураци[юя]|"
    r"контейнер|отчет|приложение|результат|сервис|систему|файл)\b",
    flags=re.IGNORECASE | re.DOTALL,
)
_ARTIFACT_CREATION_PATTERN = re.compile(
    r"\b(?:create|generate|make|render|save|создай|сделай|сгенерируй|сохрани)\b"
    r".{0,48}\b(?:analysis|approach|design|plan|strategy|summary|анализ|дизайн|план|"
    r"подход|стратегия)\b",
    flags=re.IGNORECASE | re.DOTALL,
)


def _evidence_policy(title: str, objective: str, assertion: str) -> str:
    text = " ".join((title, objective, assertion))
    if _STATE_ACTION_PATTERN.search(text):
        return "state"
    if _ARTIFACT_CREATION_PATTERN.search(text):
        return "artifact"
    if _GOAL_CREATE_STATE_PATTERN.search(text):
        return "state"
    if _ARTIFACT_GENERATION_PATTERN.search(text):
        return "artifact"
    if _OBSERVATION_PATTERN.search(text):
        return "observation"
    if _ARTIFACT_PATTERN.search(text):
        return "artifact"
    return "state"


def _goal_requires_state(goal: str) -> bool:
    return bool(
        _STATE_ACTION_PATTERN.search(goal)
        or _GOAL_CREATE_STATE_PATTERN.search(goal)
        or re.search(r"\b(?:реализуй|реализовать)\b", goal, flags=re.IGNORECASE)
    )


def _goal_required_policy(goal: str) -> str | None:
    if _goal_requires_state(goal):
        return "state"
    if _OBSERVATION_PATTERN.search(goal):
        return "observation"
    return None


def _validate_goal_evidence_coverage(goal: str, specs: tuple[StepSpec, ...]) -> None:
    required_policy = _goal_required_policy(goal)
    if required_policy is None:
        return
    matching_specs = tuple(spec for spec in specs if spec.evidence_policy == required_policy)
    if not matching_specs:
        raise ValueError(
            f"{required_policy} mission goal requires at least one independently "
            f"verified {required_policy} step"
        )
    goal_terms = _semantic_terms(goal)
    required = min(2, len(goal_terms))
    if required == 0:
        raise ValueError(f"{required_policy} mission goal has no bindable semantic subject")
    if not any(
        len(
            goal_terms
            & _semantic_terms(
                " ".join(
                    (
                        spec.title,
                        spec.objective,
                        *(criterion.description for criterion in spec.criteria),
                    )
                )
            )
        )
        >= required
        for spec in matching_specs
    ):
        raise ValueError(
            f"{required_policy} mission goal is not covered by a goal-bound "
            f"{required_policy} assertion"
        )


def _semantic_key(token: str) -> str:
    normalized = token.casefold().replace("ё", "е")
    return normalized[:6] if len(normalized) >= 7 else normalized


_ACTION_INTENT_SEMANTIC_KEYS = frozenset(
    _semantic_key(item)
    for item in {
        "apply",
        "build",
        "configure",
        "create",
        "delete",
        "deploy",
        "execute",
        "export",
        "fix",
        "generate",
        "implement",
        "install",
        "migrate",
        "modify",
        "patch",
        "provision",
        "remove",
        "render",
        "restart",
        "save",
        "start",
        "stop",
        "terminate",
        "update",
        "write",
        "внедри",
        "запусти",
        "измени",
        "исправь",
        "настрой",
        "обнови",
        "реализуй",
        "сгенерируй",
        "сделай",
        "сохрани",
        "создай",
        "удали",
        "установи",
        "экспортируй",
    }
)


def _semantic_terms(value: str) -> frozenset[str]:
    terms: set[str] = set()
    for token in re.findall(r"[^\W_]+", value, flags=re.UNICODE):
        normalized = token.casefold().replace("ё", "е")
        if len(normalized) < 4 or normalized.isdigit() or normalized in _SEMANTIC_STOPWORDS:
            continue
        terms.add(_semantic_key(normalized))
    return frozenset(terms)


def _artifact_relevance(
    spec: StepSpec,
    observed: str,
) -> tuple[frozenset[str], frozenset[str]]:
    expected_text = " ".join(
        (
            spec.title,
            spec.objective,
            *(criterion.description for criterion in spec.criteria),
        )
    )
    expected_terms = _semantic_terms(expected_text)
    return expected_terms, expected_terms & _semantic_terms(observed)


def _required_relevance_count(expected_terms: frozenset[str]) -> int:
    if not expected_terms:
        return 1
    return min(2, len(expected_terms))


def _has_required_relevance(
    expected_terms: frozenset[str],
    matched_terms: frozenset[str],
) -> bool:
    return bool(expected_terms) and len(matched_terms) >= _required_relevance_count(expected_terms)


def _strict_verification_result(value: Any) -> VerificationResult | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "ok",
        "status",
        "action_id",
        "action_kind",
        "summary",
        "evidence",
        "error",
    }
    if set(value) - allowed or not isinstance(value.get("ok"), bool):
        return None
    try:
        status = VerificationStatus(str(value.get("status") or ""))
    except ValueError:
        return None
    action_id = value.get("action_id")
    action_kind = value.get("action_kind")
    summary = value.get("summary")
    raw_evidence = value.get("evidence")
    if (
        not isinstance(action_id, str)
        or not action_id.strip()
        or not isinstance(action_kind, str)
        or not action_kind.strip()
        or not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(raw_evidence, list | tuple)
        or not raw_evidence
        or len(raw_evidence) > 256
    ):
        return None
    parsed: list[VerificationEvidence] = []
    evidence_allowed = {
        "source",
        "assertion",
        "expected",
        "observed",
        "passed",
        "captured_at",
        "error",
        "subject",
    }
    for item in raw_evidence:
        if (
            not isinstance(item, dict)
            or set(item) - evidence_allowed
            or not isinstance(item.get("source"), str)
            or not str(item.get("source") or "").strip()
            or not isinstance(item.get("assertion"), str)
            or not str(item.get("assertion") or "").strip()
            or not isinstance(item.get("passed"), bool)
            or not isinstance(item.get("captured_at"), str)
            or not str(item.get("captured_at") or "").strip()
            or item.get("error") is not None
            and not isinstance(item.get("error"), str)
            or item.get("subject") is not None
            and not isinstance(item.get("subject"), str)
        ):
            return None
        parsed.append(
            VerificationEvidence(
                source=item["source"],
                assertion=item["assertion"],
                expected=item.get("expected"),
                observed=item.get("observed"),
                passed=item["passed"],
                captured_at=item["captured_at"],
                error=item.get("error"),
                subject=item.get("subject"),
            )
        )
    error = value.get("error")
    if error is not None and not isinstance(error, str):
        return None
    try:
        return VerificationResult(
            ok=value["ok"],
            status=status,
            action_id=action_id[:256],
            action_kind=action_kind[:256],
            summary=summary[:1000],
            evidence=tuple(parsed),
            error=error[:1000] if isinstance(error, str) else None,
        )
    except (TypeError, ValueError):
        return None


def _strict_transaction_verification(
    value: Any,
    arguments: dict[str, Any],
) -> VerificationResult | None:
    actions = arguments.get("actions")
    if (
        not isinstance(value, list)
        or not isinstance(actions, list)
        or len(value) != len(actions)
        or not value
    ):
        return None
    parsed = tuple(_strict_verification_result(item) for item in value)
    if any(item is None for item in parsed):
        return None
    verified = tuple(item for item in parsed if item is not None)
    try:
        requested = tuple(parse_action(item) for item in actions)
    except (TypeError, ValueError):
        return None
    if any(
        (result.action_id, result.action_kind)
        != (action.action_id, type(action).__name__)
        for result, action in zip(verified, requested, strict=True)
    ):
        return None
    key = str(arguments.get("idempotency_key") or "").strip()
    if not key:
        return None
    passed = all(item.ok for item in verified)
    return VerificationResult(
        ok=passed,
        status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        action_id=f"transaction:{key}",
        action_kind="ExecutionTransaction",
        summary=(
            "Every transaction action passed independent verification."
            if passed
            else "At least one transaction action failed independent verification."
        ),
        evidence=tuple(
            evidence for item in verified for evidence in item.evidence
        ),
        error=next((item.error for item in verified if item.error), None),
    )


def _expected_action_identity(
    tool: str,
    arguments: dict[str, Any],
) -> tuple[str, str] | None:
    if tool == "execution.transaction":
        key = str(arguments.get("idempotency_key") or "").strip()
        actions = arguments.get("actions")
        if key and isinstance(actions, list) and actions:
            return f"transaction:{key}", "ExecutionTransaction"
        return None
    if tool != "execution.apply":
        return None
    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        action = parse_action(payload)
    except (TypeError, ValueError):
        return None
    return action.action_id, type(action).__name__


def _verification_contract(
    spec: StepSpec,
    tool: str,
    arguments: dict[str, Any],
) -> VerificationContract | None:
    if tool not in _EXECUTIVE_MUTATION_TOOLS or spec.evidence_policy != "state":
        return None
    if tool == "execution.transaction":
        canonical = _canonical_mutation_payloads(tool, arguments)
        key = str(arguments.get("idempotency_key") or "").strip()
        if not canonical or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", key):
            return None
        try:
            arguments_sha256 = _json_sha256(arguments)
            postcondition_sha256 = _json_sha256(
                {
                    "actions": canonical,
                    "verification": arguments.get("verification") or {},
                }
            )
        except (TypeError, ValueError):
            return None
        return VerificationContract(
            tool=tool,
            arguments_sha256=arguments_sha256,
            action_id=f"transaction:{key}",
            action_kind="ExecutionTransaction",
            postcondition_sha256=postcondition_sha256,
            objective_sha256=_objective_digest(spec),
        )
    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        action = parse_action(payload)
        canonical_payload = json.loads(canonical_action_json(payload))
        arguments_sha256 = _json_sha256(arguments)
        postcondition_sha256 = _json_sha256(
            {
                "payload": canonical_payload,
                "verification": arguments.get("verification") or {},
            }
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return VerificationContract(
        tool=tool,
        arguments_sha256=arguments_sha256,
        action_id=action.action_id,
        action_kind=type(action).__name__,
        postcondition_sha256=postcondition_sha256,
        objective_sha256=_objective_digest(spec),
    )


def _objective_digest(spec: StepSpec) -> str:
    return _json_sha256({"step_id": spec.step_id, "objective": spec.objective})


def _assertion_digest(spec: StepSpec) -> str:
    return _json_sha256([criterion.model_dump(mode="json") for criterion in spec.criteria])


_ACTION_SUBJECT_FIELDS = frozenset(
    {
        "argv",
        "arguments",
        "cwd",
        "destination",
        "executable",
        "host",
        "key",
        "name",
        "path",
        "pid",
        "port",
        "source",
        "target",
        "url",
        "observe_paths",
    }
)


def _canonical_execution_payload(arguments: dict[str, Any]) -> dict[str, Any] | None:
    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        return None
    action_body = payload.get("action")
    explicit_id = action_body.get("action_id") if isinstance(action_body, dict) else None
    if not isinstance(explicit_id, str) or _STEP_ID_PATTERN.fullmatch(explicit_id) is None:
        # The execution protocol may generate an id for direct interactive use,
        # but an executive approval must review and persist one stable identity.
        return None
    try:
        return json.loads(canonical_action_json(payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _canonical_mutation_payloads(
    tool: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    if tool == "execution.apply":
        payload = _canonical_execution_payload(arguments)
        return (payload,) if payload is not None else ()
    if tool != "execution.transaction":
        return ()
    actions = arguments.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 128:
        return ()
    canonical: list[dict[str, Any]] = []
    action_ids: set[str] = set()
    for item in actions:
        if not isinstance(item, dict):
            return ()
        action_body = item.get("action")
        explicit_id = action_body.get("action_id") if isinstance(action_body, dict) else None
        if (
            not isinstance(explicit_id, str)
            or _STEP_ID_PATTERN.fullmatch(explicit_id) is None
            or explicit_id in action_ids
        ):
            return ()
        try:
            action = parse_action(item)
            if (
                classify_payload(item) is not ActionClass.MUTATION
                or action.action_id != explicit_id
            ):
                return ()
            action_ids.add(explicit_id)
            canonical.append(json.loads(canonical_action_json(item)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
    return tuple(canonical)


def _mutation_subject_values(canonical_payload: dict[str, Any]) -> frozenset[str]:
    action = canonical_payload.get("action")
    if not isinstance(action, dict):
        return frozenset()
    subject = {key: value for key, value in action.items() if key in _ACTION_SUBJECT_FIELDS}
    return _subject_scalar_values(subject)


def _mutation_primary_targets(canonical_payload: dict[str, Any]) -> frozenset[str]:
    action = canonical_payload.get("action")
    if not isinstance(action, dict):
        return frozenset()
    kind = str(action.get("kind") or "")
    if kind in {"fs.copy", "fs.move"}:
        return frozenset(
            str(action.get(field) or "").strip()
            for field in ("source", "destination")
            if str(action.get(field) or "").strip()
        )
    field = (
        "path"
        if kind in {"fs.delete", "fs.mkdir", "fs.write"}
        else ""
    )
    value = action.get(field) if field else None
    return frozenset({str(value).strip()}) if str(value or "").strip() else frozenset()


def _mutation_actions_are_plan_bound(
    planner: AdaptiveDAGPlanner,
    spec: StepSpec,
    tool: str,
    canonical_payloads: tuple[dict[str, Any], ...],
) -> bool:
    """Bind one action directly or an atomic batch collectively to its plan.

    A transaction may intentionally split multiple literal targets across its
    actions.  The batch is accepted only when the union covers every declared
    anchor and every otherwise-unbound mutation target is itself one of those
    anchors.  This prevents an approved batch from smuggling an unrelated third
    mutation while preserving exact source/destination roles for copy and move.
    """

    explicit_kinds = _explicit_action_kinds(spec)
    action_kinds = {
        str(action.get("kind") or "")
        for payload in canonical_payloads
        if isinstance((action := payload.get("action")), dict)
    }
    if (
        tool == "execution.transaction"
        and len(explicit_kinds) > 1
        and len(action_kinds) > 1
    ):
        # Free-form prose does not encode a durable target-to-effect mapping.
        # Mixed-effect work must be decomposed into typed DAG steps rather than
        # authorizing a transaction where operations can be swapped by target.
        return False
    if not all(
        _action_kind_matches_explicit_intent(spec, payload)
        for payload in canonical_payloads
    ):
        return False
    bound = tuple(
        _mutation_action_is_plan_bound(spec, payload)
        or _mutation_action_matches_discovered_subject(planner, spec, payload)
        for payload in canonical_payloads
    )
    if all(bound):
        return True
    if tool != "execution.transaction":
        return False
    anchors = _literal_inspection_anchors(spec)
    if not anchors:
        return False
    all_subjects = frozenset(
        subject
        for payload in canonical_payloads
        for subject in _mutation_subject_values(payload)
    )
    if not _anchors_match_subjects(anchors, all_subjects):
        return False
    for already_bound, payload in zip(bound, canonical_payloads, strict=True):
        if already_bound:
            continue
        action = payload.get("action")
        if (
            isinstance(action, dict)
            and action.get("kind") in {"fs.copy", "fs.move"}
            and len(_ordered_path_anchors(spec)) >= 2
        ):
            # Ordered source/destination semantics cannot be weakened to
            # unordered transaction-wide anchor membership.
            return False
        targets = _mutation_primary_targets(payload)
        if not targets or not all(
            any(_anchor_matches_subject(anchor, target) for anchor in anchors)
            for target in targets
        ):
            return False
    return True


def _action_kind_matches_explicit_intent(
    spec: StepSpec,
    canonical_payload: dict[str, Any],
) -> bool:
    action = canonical_payload.get("action")
    if not isinstance(action, dict):
        return False
    if action.get("kind") == "fs.move" and re.fullmatch(
        r"[0-9a-fA-F]{64}",
        str(action.get("expected_sha256") or ""),
    ) is None:
        # A post-crash move cannot prove destination identity once the source
        # is absent unless its reviewed source digest was bound up front.
        return False
    allowed = _explicit_action_kinds(spec)
    kind = str(action.get("kind") or "")
    if kind in {"fs.delete", "fs.move", "process.terminate", "registry.delete"}:
        return kind in allowed
    return not allowed or kind in allowed


def _explicit_action_kinds(spec: StepSpec) -> frozenset[str]:
    """Map only unambiguous operator verbs to typed mutation kinds.

    Ambiguous implementation goals intentionally return no restriction. Exact
    filesystem/process/registry intents fail closed so a verified opposite
    operation cannot satisfy the same subject-bound DAG assertion.
    """

    text = _mask_action_intent_literals(" ".join((spec.title, spec.objective)).casefold())
    file_noun = (
        r"(?:file|artifact|path|config(?:uration)?|document|"
        r"\u0444\u0430\u0439\u043b|\u0430\u0440\u0442\u0435\u0444\u0430\u043a\u0442|"
        r"\u043f\u0443\u0442\u044c|\u043a\u043e\u043d\u0444\u0438\u0433(?:\u0443\u0440\u0430\u0446\u0438\u044f)?|"
        r"\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442)"
    )
    directory_noun = (
        r"(?:directory|folder|\u043a\u0430\u0442\u0430\u043b\u043e\u0433|"
        r"\u043f\u0430\u043f\u043a\u0430|\u0434\u0438\u0440\u0435\u043a\u0442\u043e\u0440\u0438\u044f)"
    )
    process_noun = (
        r"(?:process|pid|program|executable|script|command|service|"
        r"\u043f\u0440\u043e\u0446\u0435\u0441\u0441|\u043f\u0440\u043e\u0433\u0440\u0430\u043c\u043c\u0430|"
        r"\u0441\u043a\u0440\u0438\u043f\u0442|\u043a\u043e\u043c\u0430\u043d\u0434\u0430|\u0441\u0435\u0440\u0432\u0438\u0441)"
    )
    registry_noun = (
        r"(?:registry|hkey|reg(?:istry)?\s+(?:key|value)|"
        r"\u0440\u0435\u0435\u0441\u0442\u0440|\u043a\u043b\u044e\u0447\s+\u0440\u0435\u0435\u0441\u0442\u0440\u0430|"
        r"\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435\s+\u0440\u0435\u0435\u0441\u0442\u0440\u0430)"
    )
    artifact_output = (
        r"(?:csv|docx?|output|pdf|report|spreadsheet|workbook|xlsx?|"
        r"\u0432\u044b\u0432\u043e\u0434|\u043e\u0442\u0447\u0435\u0442|\u0442\u0430\u0431\u043b\u0438\u0446\u0430)"
    )
    file_target = rf"(?:{file_noun}|{artifact_output}|__file_target__)"
    any_path_target = rf"(?:{file_target}|{directory_noun}|__path_target__)"

    def explicit(verbs: str, subject: str) -> bool:
        return bool(re.search(rf"\b(?:{verbs})\b.{{0,64}}(?:{subject})\b", text))

    def direct(verbs: str, subject: str) -> bool:
        qualifiers = (
            r"(?:(?:the|exact|target|planned|source|destination|"
            r"\u044d\u0442\u043e\u0442|\u0442\u043e\u0447\u043d\u044b\u0439|"
            r"\u0446\u0435\u043b\u0435\u0432\u043e\u0439)\s+)*"
        )
        return bool(re.search(rf"\b(?:{verbs})\b\s+{qualifiers}(?:{subject})\b", text))

    allowed: set[str] = set()
    create_verbs = (
        r"create|make|mkdir|"
        r"\u0441\u043e\u0437\u0434\u0430(?:\u0442\u044c|\u0439|\u0432\u0430\u0442\u044c)|"
        r"\u0441\u0434\u0435\u043b\u0430(?:\u0442\u044c|\u0439)"
    )
    directory_create = explicit(
        r"mkdir|make\s+(?:a\s+)?directory|create|"
        r"\u0441\u043e\u0437\u0434\u0430(?:\u0442\u044c|\u0439|\u0432\u0430\u0442\u044c)",
        directory_noun,
    )
    if directory_create:
        allowed.add("fs.mkdir")
    write_verbs = (
        r"write|overwrite|save|update|modify|patch|fix|"
        r"\u0437\u0430\u043f\u0438\u0441(?:\u0430\u0442\u044c|\u0430\u0442\u044b\u0432\u0430\u0442\u044c)|"
        r"\u0441\u043e\u0445\u0440\u0430\u043d(?:\u0438\u0442\u044c|\u0438)|"
        r"\u043e\u0431\u043d\u043e\u0432(?:\u0438\u0442\u044c|\u0438)|"
        r"\u0438\u0437\u043c\u0435\u043d(?:\u0438\u0442\u044c|\u0438)|"
        r"\u0438\u0441\u043f\u0440\u0430\u0432(?:\u0438\u0442\u044c|\u044c)"
    )
    output_verbs = (
        rf"{create_verbs}|generate|render|export|"
        r"\u0441\u0433\u0435\u043d\u0435\u0440\u0438\u0440(?:\u043e\u0432\u0430\u0442\u044c|\u0443\u0439)|"
        r"\u044d\u043a\u0441\u043f\u043e\u0440\u0442\u0438\u0440(?:\u043e\u0432\u0430\u0442\u044c|\u0443\u0439)"
    )
    explicit_file_output = explicit(output_verbs, file_target) and not directory_create
    if explicit(write_verbs, any_path_target) or explicit_file_output:
        allowed.add("fs.write")
    if (
        explicit(create_verbs, r"__path_target__")
        and not directory_create
        and not explicit_file_output
        and not explicit(create_verbs, file_noun)
    ):
        # A suffix-less path is ambiguous between a file and a directory, but
        # it is still an explicit creation intent and must never authorize a
        # destructive or process action.
        allowed.update({"fs.mkdir", "fs.write"})
    if direct(
        r"delete|unlink|erase|remove|\u0443\u0434\u0430\u043b(?:\u0438\u0442\u044c|\u0438)",
        any_path_target,
    ):
        allowed.add("fs.delete")
    if direct(
        r"copy|duplicate|\u043a\u043e\u043f\u0438\u0440(?:\u043e\u0432\u0430\u0442\u044c|\u0443\u0439)",
        any_path_target,
    ):
        allowed.add("fs.copy")
    if direct(
        r"move|rename|\u043f\u0435\u0440\u0435\u043c\u0435\u0441\u0442(?:\u0438\u0442\u044c|\u0438)|"
        r"\u043f\u0435\u0440\u0435\u0438\u043c\u0435\u043d(?:\u043e\u0432\u0430\u0442\u044c|\u0443\u0439)",
        any_path_target,
    ):
        allowed.add("fs.move")
    if explicit(
        r"run|execute|launch|start|\u0437\u0430\u043f\u0443\u0441\u0442(?:\u0438\u0442\u044c|\u0438)|"
        r"\u0432\u044b\u043f\u043e\u043b\u043d(?:\u0438\u0442\u044c|\u0438)",
        rf"(?:{process_noun}|__file_target__|__path_target__)",
    ):
        allowed.add("process.run")
    if explicit(
        r"terminate|kill|stop|\u043e\u0441\u0442\u0430\u043d\u043e\u0432(?:\u0438\u0442\u044c|\u0438)|"
        r"\u0437\u0430\u0432\u0435\u0440\u0448(?:\u0438\u0442\u044c|\u0438)",
        process_noun,
    ):
        allowed.add("process.terminate")
    registry_context = bool(re.search(rf"\b{registry_noun}\b", text))
    if registry_context and re.search(
        r"\b(?:set|write|create|update|\u0443\u0441\u0442\u0430\u043d\u043e\u0432(?:\u0438\u0442\u044c|\u0438)|"
        r"\u0437\u0430\u043f\u0438\u0441(?:\u0430\u0442\u044c|\u0438))\b",
        text,
    ):
        allowed.add("registry.set")
    if registry_context and re.search(
        r"\b(?:delete|remove|\u0443\u0434\u0430\u043b(?:\u0438\u0442\u044c|\u0438))\b",
        text,
    ):
        allowed.add("registry.delete")
    return frozenset(allowed)


def _mask_action_intent_literals(value: str) -> str:
    """Remove verb-like path components before interpreting operation intent."""

    file_suffix = re.compile(
        r"\.(?:cfg|conf|css|csv|docx?|html?|ini|js|json|log|md|pdf|py|sh|toml|ts|"
        r"txt|xlsx?|xml|yaml|yml)$",
        flags=re.IGNORECASE,
    )

    def marker_for(candidate: str) -> str:
        candidate = candidate.rstrip(".,;:")
        marker = "__file_target__" if file_suffix.search(candidate) else "__path_target__"
        return f" {marker} "

    masked = value
    for start, end, candidate in reversed(_declared_path_spans(value)):
        masked = masked[:start] + marker_for(candidate) + masked[end:]
    masked = re.sub(r"https?://[^\s\]\[(){}<>\"']+", " __url_target__ ", masked)
    masked = re.sub(
        r"\b[a-z0-9_.-]+\.(?:cfg|conf|css|csv|docx?|html?|ini|js|json|log|md|pdf|py|sh|"
        r"toml|ts|txt|xlsx?|xml|yaml|yml)\b",
        " __file_target__ ",
        masked,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", masked).strip()


def _mutation_action_is_plan_bound(
    spec: StepSpec,
    canonical_payload: dict[str, Any],
) -> bool:
    action = canonical_payload.get("action")
    if not isinstance(action, dict):
        return False
    subject = {key: value for key, value in action.items() if key in _ACTION_SUBJECT_FIELDS}
    try:
        subject_text = json.dumps(
            subject,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False
    subject_values = _subject_scalar_values(subject)
    if action.get("kind") == "process.run":
        if not _process_command_is_plan_bound(spec, action):
            return False
        planned_ports = _planned_tcp_ports(spec)
        command_values = _subject_scalar_values(
            {
                "executable": action.get("executable"),
                "arguments": action.get("arguments"),
            }
        )
        if planned_ports and not all(
            any(_anchor_matches_subject(str(port), value) for value in command_values)
            for port in planned_ports
        ):
            return False
    if action.get("kind") == "process.terminate":
        planned_pids = _planned_process_ids(spec)
        if planned_pids and action.get("pid") not in planned_pids:
            return False
        if _planned_tcp_ports(spec) and not planned_pids:
            # A TCP port is not a process identity. A preceding trusted
            # inspection must first convert it to a concrete owned PID step.
            return False
    if action.get("kind") in {"fs.copy", "fs.move"}:
        ordered_paths = _ordered_path_anchors(spec)
        if len(ordered_paths) >= 2:
            return bool(
                _anchor_matches_subject(
                    ordered_paths[0],
                    str(action.get("source") or ""),
                )
                and _anchor_matches_subject(
                    ordered_paths[-1],
                    str(action.get("destination") or ""),
                )
            )
    anchors = _literal_inspection_anchors(spec)
    if anchors:
        return _anchors_match_subjects(anchors, subject_values)
    expected_text = " ".join((spec.title, spec.objective))
    expected_terms = _semantic_terms(expected_text) - _ACTION_INTENT_SEMANTIC_KEYS
    matched_terms = expected_terms & _semantic_terms(subject_text)
    return bool(expected_terms) and len(matched_terms) >= min(2, len(expected_terms))


def _process_command_is_plan_bound(spec: StepSpec, action: dict[str, Any]) -> bool:
    executable = str(action.get("executable") or "").strip()
    ordered_paths = _ordered_path_anchors(spec)
    if (
        executable
        and len(ordered_paths) == 1
        and _anchor_matches_subject(ordered_paths[0], executable)
    ):
        return True
    command_values = _subject_scalar_values(
        {
            "executable": executable,
            "arguments": action.get("arguments"),
        }
    )
    if not command_values:
        return False
    expected_terms = (
        _semantic_terms(" ".join((spec.title, spec.objective)))
        - _ACTION_INTENT_SEMANTIC_KEYS
    )
    for anchor in _literal_inspection_anchors(spec):
        expected_terms -= _semantic_terms(anchor)
    command_terms = _semantic_terms(" ".join(sorted(command_values)))
    return bool(expected_terms & command_terms)


def _mutation_action_matches_discovered_subject(
    planner: AdaptiveDAGPlanner,
    spec: StepSpec,
    canonical_payload: dict[str, Any],
) -> bool:
    action_hashes = {
        _json_sha256(_normalise_subject(value))
        for value in _mutation_primary_targets(canonical_payload)
    }
    if not action_hashes:
        return False
    snapshot = planner.snapshot()
    steps = {item.spec.step_id: item for item in snapshot.steps}
    discovered: set[str] = set()
    pending = list(spec.dependencies)
    visited: set[str] = set()
    while pending:
        step_id = pending.pop()
        if step_id in visited:
            continue
        visited.add(step_id)
        predecessor = steps.get(step_id)
        if predecessor is None:
            continue
        pending.extend(predecessor.spec.dependencies)
        if predecessor.status is not StepStatus.SUCCEEDED:
            continue
        verification = predecessor.action_evidence.get("state_verification")
        if not isinstance(verification, dict):
            continue
        evidence = verification.get("evidence")
        action = verification.get("action")
        action_tool = action.get("tool") if isinstance(action, dict) else None
        if (
            verification.get("inspector")
            not in {"read_only_artifact_inspector", "state_verifier+read_only_scope"}
            or not isinstance(action_tool, str)
            or not action_tool
        ):
            continue
        for item in evidence or []:
            if (
                not isinstance(item, dict)
                or item.get("source") != "executive.read_only_artifact"
                or item.get("passed") is not True
                or item.get("subject") != action_tool
            ):
                continue
            observed = item.get("observed")
            if not isinstance(observed, dict) or observed.get("tool") != action_tool:
                continue
            hashes = observed.get("subject_sha256") if isinstance(observed, dict) else None
            if isinstance(hashes, list):
                discovered.update(
                    str(value)
                    for value in hashes
                    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                )
    return bool(action_hashes) and action_hashes <= discovered


def _process_postcondition_is_plan_bound(
    spec: StepSpec,
    canonical_payload: dict[str, Any],
    verification: Any,
) -> bool:
    action = canonical_payload.get("action")
    if not isinstance(action, dict) or action.get("kind") not in {
        "process.run",
        "process.terminate",
    }:
        return True
    if not isinstance(verification, dict):
        return False
    planned_ports = _planned_tcp_ports(spec)
    if planned_ports:
        tcp = verification.get("tcp")
        expected_reachable = action.get("kind") == "process.run"
        if not isinstance(tcp, list) or not all(
            any(
                isinstance(item, dict)
                and item.get("port") == port
                and item.get("reachable", True) is expected_reachable
                for item in tcp
            )
            for port in planned_ports
        ):
            return False
    if action.get("kind") == "process.terminate":
        expected_session = str(action.get("session_id") or "")
        expected_pid = action.get("pid")
        processes = verification.get("processes")
        return bool(
            expected_session
            and isinstance(expected_pid, int)
            and isinstance(processes, list)
            and any(
                isinstance(item, dict)
                and str(item.get("session_id") or "") == expected_session
                and item.get("pid") == expected_pid
                for item in processes
            )
        )
    action_subjects = _mutation_subject_values(canonical_payload)
    verification_subjects = _authoritative_subject_values(verification)
    if not verification_subjects:
        return False
    anchors = _literal_inspection_anchors(spec)
    if anchors:
        return _anchors_match_subjects(anchors, verification_subjects)
    action_normalised = {_normalise_subject(value) for value in action_subjects}
    verification_normalised = {_normalise_subject(value) for value in verification_subjects}
    if action_normalised & verification_normalised:
        return True
    expected_terms = (
        _semantic_terms(" ".join((spec.title, spec.objective))) - _ACTION_INTENT_SEMANTIC_KEYS
    )
    observed_terms = _semantic_terms(" ".join(sorted(verification_subjects)))
    return bool(expected_terms & observed_terms)


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _requires_reconcile_only_recovery(result: ToolRunResponse) -> bool:
    data = result.data if isinstance(result.data, dict) else {}
    candidates: list[dict[str, Any]] = [data]
    direct_result = data.get("result")
    if isinstance(direct_result, dict):
        candidates.append(direct_result)
    approved = data.get("approved_tool")
    if isinstance(approved, dict):
        approved_data = approved.get("data")
        if isinstance(approved_data, dict):
            candidates.append(approved_data)
            approved_result = approved_data.get("result")
            if isinstance(approved_result, dict):
                candidates.append(approved_result)
    return any(
        candidate.get("transaction_status") == "rollback_failed"
        or bool(candidate.get("rollback_errors"))
        for candidate in candidates
    )


def _contract_digest(contract: VerificationContract | None) -> str | None:
    if contract is None:
        return None
    return _json_sha256(contract.model_dump(mode="json"))


def _planned_action_digest(
    spec: StepSpec,
    bound_action: ActionCall | None = None,
) -> str:
    payload = (bound_action or spec.action).model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
