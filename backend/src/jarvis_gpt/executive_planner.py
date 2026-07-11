"""Deterministic, adaptive DAG planner for the executive execution loop.

The planner deliberately does not execute tools or ask an LLM to plan.  It owns
the strict boundary between an untrusted decomposition proposal and the
deterministic runtime state used by the executor: graph validation, bounded
retries/revisions, precondition fingerprints, state transitions, assertion
gating, and serialisable snapshots.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

PLANNER_PROTOCOL = "jarvis.planner.v1"
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_MAX_JSON_BYTES = 4 * 1024 * 1024


class PlannerError(RuntimeError):
    """Base class for deterministic planner failures."""


class GraphValidationError(PlannerError):
    """The proposed graph is incomplete, inconsistent, or cyclic."""


class InvalidTransitionError(PlannerError):
    """A goal or step state transition is not legal."""


class RevisionConflictError(PlannerError):
    """A graph revision was based on stale state or mutates protected work."""


class AttemptLimitError(PlannerError):
    """A per-step or plan-wide execution bound was reached."""


class PreconditionMismatchError(PlannerError):
    """The current environment no longer satisfies a step fingerprint."""


class VerificationError(PlannerError):
    """Verification evidence does not cover the declared assertions."""


class GoalStatus(StrEnum):
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    REVISING = "revising"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _identifier(value: str, field_name: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} contains unsupported characters")
    return value


def _nonblank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be blank")
    return value


def _normalise_json(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Return a detached, canonical JSON value and reject ambiguous payloads."""

    if budget is None:
        budget = [20_000]
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("JSON payload exceeds the 20000-item limit")
    if depth > 20:
        raise ValueError("JSON payload exceeds the maximum nesting depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError("JSON integers must fit a signed 64-bit value")
        return value
    if isinstance(value, str):
        if len(value) > 262_144:
            raise ValueError("JSON string exceeds the 262144-character limit")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list | tuple):
        return [_normalise_json(item, depth=depth + 1, budget=budget) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {
            key: _normalise_json(value[key], depth=depth + 1, budget=budget)
            for key in sorted(value)
        }
    raise ValueError(f"unsupported JSON value type: {type(value).__name__}")


def _json_object(value: dict[str, Any]) -> dict[str, Any]:
    normalised = _normalise_json(value)
    if not isinstance(normalised, dict):  # defensive; pydantic supplies a dict
        raise ValueError("value must be a JSON object")
    if len(_canonical_bytes(normalised)) > _MAX_JSON_BYTES:
        raise ValueError("JSON object exceeds the 4 MiB encoded limit")
    return normalised


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalise_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


class AssertionCriterion(_StrictModel):
    assertion_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    inspector: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    required: StrictBool = True

    @field_validator("assertion_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _identifier(value, "assertion_id")

    @field_validator("description", "inspector")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("arguments", "expected")
    @classmethod
    def validate_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value)


class ActionCall(_StrictModel):
    tool: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)
    destructive: StrictBool = False

    @field_validator("tool")
    @classmethod
    def validate_tool(cls, value: str) -> str:
        return _nonblank(value, "tool")

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value)


class EnvironmentFingerprint(_StrictModel):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: str = Field(min_length=20, max_length=40)
    facts: dict[str, Any]

    @field_validator("facts")
    @classmethod
    def validate_facts(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value)

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        self.assert_integrity()
        _parse_timestamp(self.captured_at, "captured_at")
        return self

    def assert_integrity(self) -> None:
        if _digest(self.facts) != self.digest:
            raise ValueError("environment digest does not match facts")

    @classmethod
    def capture(
        cls, facts: dict[str, Any], *, captured_at: str | None = None
    ) -> EnvironmentFingerprint:
        clean = _json_object(facts)
        return cls(
            digest=_digest(clean),
            captured_at=captured_at or _utc_now(),
            facts=clean,
        )


class PreconditionFingerprint(_StrictModel):
    name: str = Field(min_length=1, max_length=128)
    fact_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _identifier(value, "precondition name")

    @field_validator("fact_paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not path.startswith("/") or len(path) > 1024 or re.search(r"~(?:[^01]|$)", path)
            for path in value
        ):
            raise ValueError("fact paths must be bounded RFC 6901 JSON pointers")
        if len(set(value)) != len(value):
            raise ValueError("fact paths must be unique")
        return tuple(sorted(value))

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return value.strip()

    @classmethod
    def from_environment(
        cls,
        *,
        name: str,
        environment: EnvironmentFingerprint,
        fact_paths: tuple[str, ...],
        description: str = "",
    ) -> PreconditionFingerprint:
        canonical_paths = tuple(sorted(fact_paths))
        return cls(
            name=name,
            fact_paths=canonical_paths,
            expected_digest=_projected_digest(environment.facts, canonical_paths),
            description=description,
        )

    def matches(self, environment: EnvironmentFingerprint) -> bool:
        environment.assert_integrity()
        return _projected_digest(environment.facts, self.fact_paths) == self.expected_digest


class GoalDefinition(_StrictModel):
    goal_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=8000)
    criteria: tuple[AssertionCriterion, ...] = Field(min_length=1, max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("goal_id")
    @classmethod
    def validate_goal_id(cls, value: str) -> str:
        return _identifier(value, "goal_id")

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        return _nonblank(value, "objective")

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value)

    @model_validator(mode="after")
    def validate_criteria(self) -> Self:
        _require_unique(
            (criterion.assertion_id for criterion in self.criteria), "goal assertion ids"
        )
        return self


class PlannerLimits(_StrictModel):
    max_steps: StrictInt = Field(default=256, ge=1, le=4096)
    max_revisions: StrictInt = Field(default=16, ge=0, le=256)
    max_total_attempts: StrictInt = Field(default=1024, ge=1, le=100_000)
    max_step_attempts: StrictInt = Field(default=3, ge=1, le=32)
    max_revision_history: StrictInt = Field(default=64, ge=1, le=1024)


class ToolDescriptor(_StrictModel):
    name: str = Field(min_length=1, max_length=160)
    input_schema_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    read_only: StrictBool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _nonblank(value, "tool name")


class PlaybookHint(_StrictModel):
    playbook_id: str = Field(min_length=1, max_length=128)
    symptom: str = Field(min_length=1, max_length=2000)
    solution: str = Field(min_length=1, max_length=4000)
    verification: str = Field(min_length=1, max_length=2000)
    confidence_milli: StrictInt = Field(default=500, ge=0, le=1000)

    @field_validator("playbook_id")
    @classmethod
    def validate_playbook_id(cls, value: str) -> str:
        return _identifier(value, "playbook_id")


class StepSpec(_StrictModel):
    step_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=4000)
    action: ActionCall
    dependencies: tuple[str, ...] = Field(default=(), max_length=256)
    criteria: tuple[AssertionCriterion, ...] = Field(min_length=1, max_length=64)
    preconditions: tuple[PreconditionFingerprint, ...] = Field(default=(), max_length=64)
    evidence_policy: Literal["artifact", "observation", "state"] = "state"
    max_attempts: StrictInt | None = Field(default=None, ge=1, le=32)

    @field_validator("step_id")
    @classmethod
    def validate_step_id(cls, value: str) -> str:
        return _identifier(value, "step_id")

    @field_validator("title", "objective")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for dependency in value:
            _identifier(dependency, "dependency")
        if len(set(value)) != len(value):
            raise ValueError("dependencies must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_collections(self) -> Self:
        if self.step_id in self.dependencies:
            raise ValueError("a step cannot depend on itself")
        _require_unique(
            (criterion.assertion_id for criterion in self.criteria), "step assertion ids"
        )
        _require_unique((item.name for item in self.preconditions), "precondition names")
        return self


class GoalDecompositionRequest(_StrictModel):
    protocol: Literal["jarvis.planner.v1"] = PLANNER_PROTOCOL
    goal: GoalDefinition
    environment: EnvironmentFingerprint
    available_tools: tuple[ToolDescriptor, ...] = Field(default=(), max_length=1024)
    playbooks: tuple[PlaybookHint, ...] = Field(default=(), max_length=128)
    limits: PlannerLimits = Field(default_factory=PlannerLimits)

    @model_validator(mode="after")
    def validate_unique_entries(self) -> Self:
        _require_unique((tool.name for tool in self.available_tools), "tool names")
        _require_unique((item.playbook_id for item in self.playbooks), "playbook ids")
        return self


class DecompositionProposal(_StrictModel):
    protocol: Literal["jarvis.planner.v1"] = PLANNER_PROTOCOL
    goal_id: str = Field(min_length=1, max_length=128)
    steps: tuple[StepSpec, ...] = Field(min_length=1, max_length=4096)
    rationale: str = Field(default="", max_length=8000)

    @field_validator("goal_id")
    @classmethod
    def validate_goal_id(cls, value: str) -> str:
        return _identifier(value, "goal_id")

    @model_validator(mode="after")
    def validate_unique_steps(self) -> Self:
        _require_unique((step.step_id for step in self.steps), "step ids")
        return self


class PlanRevision(_StrictModel):
    protocol: Literal["jarvis.planner.v1"] = PLANNER_PROTOCOL
    revision_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    base_revision: StrictInt = Field(ge=0)
    reason: str = Field(min_length=1, max_length=4000)
    environment: EnvironmentFingerprint
    add_steps: tuple[StepSpec, ...] = Field(default=(), max_length=4096)
    replace_steps: tuple[StepSpec, ...] = Field(default=(), max_length=4096)
    remove_step_ids: tuple[str, ...] = Field(default=(), max_length=4096)
    reset_step_ids: tuple[str, ...] = Field(default=(), max_length=4096)

    @field_validator("revision_id", "goal_id")
    @classmethod
    def validate_ids(cls, value: str, info: Any) -> str:
        return _identifier(value, info.field_name)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return _nonblank(value, "reason")

    @field_validator("remove_step_ids", "reset_step_ids")
    @classmethod
    def validate_step_ids(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        for step_id in value:
            _identifier(step_id, info.field_name)
        if len(set(value)) != len(value):
            raise ValueError(f"{info.field_name} must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        added = {step.step_id for step in self.add_steps}
        replaced = {step.step_id for step in self.replace_steps}
        removed = set(self.remove_step_ids)
        reset = set(self.reset_step_ids)
        _require_unique((step.step_id for step in self.add_steps), "added step ids")
        _require_unique((step.step_id for step in self.replace_steps), "replacement step ids")
        if added & replaced or added & removed or replaced & removed:
            raise ValueError("add, replace, and remove step sets must be disjoint")
        if reset & removed:
            raise ValueError("removed steps cannot also be reset")
        if not (added or replaced or removed or self.reset_step_ids):
            raise ValueError("revision must contain at least one graph or state change")
        return self


class AssertionResult(_StrictModel):
    assertion_id: str = Field(min_length=1, max_length=128)
    inspector: str = Field(min_length=1, max_length=160)
    passed: StrictBool
    evidence: dict[str, Any] = Field(default_factory=dict)
    checked_at: str = Field(default_factory=lambda: _utc_now(), min_length=20, max_length=40)

    @field_validator("assertion_id")
    @classmethod
    def validate_assertion_id(cls, value: str) -> str:
        return _identifier(value, "assertion_id")

    @field_validator("inspector")
    @classmethod
    def validate_inspector(cls, value: str) -> str:
        return _nonblank(value, "inspector")

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value)

    @model_validator(mode="after")
    def validate_timestamp(self) -> Self:
        _parse_timestamp(self.checked_at, "checked_at")
        return self


class VerificationContract(_StrictModel):
    """Exact, durable postcondition contract for one active step attempt."""

    protocol: Literal["jarvis.step-verification-contract.v1"] = (
        "jarvis.step-verification-contract.v1"
    )
    tool: str = Field(min_length=1, max_length=160)
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_id: str = Field(min_length=1, max_length=256)
    action_kind: str = Field(min_length=1, max_length=256)
    postcondition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objective_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("tool", "action_id", "action_kind")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class StepSnapshot(_StrictModel):
    spec: StepSpec
    status: StepStatus
    attempts: StrictInt = Field(ge=0)
    started_at: str | None = None
    started_environment_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    finished_at: str | None = None
    action_evidence: dict[str, Any] = Field(default_factory=dict)
    bound_action: ActionCall | None = None
    verification_contract: VerificationContract | None = None
    assertion_results: tuple[AssertionResult, ...] = ()
    last_error: str | None = Field(default=None, max_length=4000)

    @field_validator("action_evidence")
    @classmethod
    def validate_action_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value)

    @model_validator(mode="after")
    def validate_runtime_shape(self) -> Self:
        if self.started_at is not None:
            _parse_timestamp(self.started_at, "started_at")
        if self.finished_at is not None:
            _parse_timestamp(self.finished_at, "finished_at")
        if self.attempts == 0 and self.started_at is not None:
            raise ValueError("an unattempted step cannot have a start timestamp")
        if (self.started_at is None) != (self.started_environment_digest is None):
            raise ValueError(
                "step start timestamp and environment digest must be recorded together"
            )
        if self.status in {StepStatus.RUNNING, StepStatus.VERIFYING} and (
            self.started_at is None or self.finished_at is not None
        ):
            raise ValueError("an active step requires only a start timestamp")
        if (
            self.status
            in {
                StepStatus.SUCCEEDED,
                StepStatus.FAILED,
                StepStatus.CANCELLED,
            }
            and self.finished_at is None
        ):
            raise ValueError("a terminal step requires a finish timestamp")
        if self.status in {StepStatus.SUCCEEDED, StepStatus.FAILED} and (
            self.attempts == 0 or self.started_at is None
        ):
            raise ValueError("a completed step requires an execution attempt")
        if (self.bound_action is None) != (self.verification_contract is None):
            raise ValueError("a bound execution action and its verification contract must coexist")
        if (
            self.bound_action is not None
            and self.verification_contract is not None
            and (
                self.bound_action.tool != self.verification_contract.tool
                or _digest(self.bound_action.arguments)
                != self.verification_contract.arguments_sha256
            )
        ):
            raise ValueError("bound execution action does not match its contract")
        return self


class RevisionSnapshot(_StrictModel):
    revision_id: str
    revision: StrictInt = Field(ge=1)
    reason: str
    environment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    applied_at: str
    added: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    reset: tuple[str, ...] = ()

    @field_validator("revision_id")
    @classmethod
    def validate_revision_id(cls, value: str) -> str:
        return _identifier(value, "revision_id")

    @model_validator(mode="after")
    def validate_applied_at(self) -> Self:
        _parse_timestamp(self.applied_at, "applied_at")
        return self


class PlannerSnapshot(_StrictModel):
    protocol: Literal["jarvis.planner.v1"] = PLANNER_PROTOCOL
    goal: GoalDefinition
    limits: PlannerLimits
    status: GoalStatus
    revision: StrictInt = Field(ge=0)
    total_attempts: StrictInt = Field(ge=0)
    retired_attempts: StrictInt = Field(default=0, ge=0)
    environment: EnvironmentFingerprint
    steps: tuple[StepSnapshot, ...]
    topological_order: tuple[str, ...]
    ready_step_ids: tuple[str, ...]
    goal_assertion_results: tuple[AssertionResult, ...] = ()
    revision_history: tuple[RevisionSnapshot, ...] = ()
    failure_reason: str | None = None
    updated_at: str

    @model_validator(mode="after")
    def validate_snapshot_shape(self) -> Self:
        _parse_timestamp(self.updated_at, "updated_at")
        step_ids = tuple(item.spec.step_id for item in self.steps)
        _require_unique(step_ids, "snapshot step ids")
        _require_unique(self.topological_order, "snapshot topological ids")
        _require_unique(
            (item.revision_id for item in self.revision_history),
            "snapshot revision ids",
        )
        if self.total_attempts < self.retired_attempts:
            raise ValueError("retired attempts cannot exceed total attempts")
        return self


@dataclass(slots=True)
class _StepRuntime:
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: str | None = None
    started_environment_digest: str | None = None
    finished_at: str | None = None
    action_evidence: dict[str, Any] = field(default_factory=dict)
    bound_action: ActionCall | None = None
    verification_contract: VerificationContract | None = None
    assertion_results: tuple[AssertionResult, ...] = ()
    last_error: str | None = None


class AdaptiveDAGPlanner:
    """Thread-safe state machine around a validated, dynamically revisable DAG."""

    def __init__(
        self,
        *,
        goal: GoalDefinition,
        environment: EnvironmentFingerprint,
        limits: PlannerLimits | None = None,
    ) -> None:
        self._goal = GoalDefinition.model_validate(goal.model_dump())
        self._limits = PlannerLimits.model_validate((limits or PlannerLimits()).model_dump())
        self._environment = EnvironmentFingerprint.model_validate(environment.model_dump())
        self._status = GoalStatus.PLANNING
        self._specs: dict[str, StepSpec] = {}
        self._runtime: dict[str, _StepRuntime] = {}
        self._topological_order: tuple[str, ...] = ()
        self._revision = 0
        self._total_attempts = 0
        self._revision_history: list[RevisionSnapshot] = []
        self._goal_assertion_results: tuple[AssertionResult, ...] = ()
        self._failure_reason: str | None = None
        self._updated_at = _utc_now()
        self._lock = threading.RLock()

    @property
    def status(self) -> GoalStatus:
        with self._lock:
            return self._status

    @property
    def goal(self) -> GoalDefinition:
        with self._lock:
            return self._goal.model_copy(deep=True)

    @property
    def limits(self) -> PlannerLimits:
        return self._limits

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def decomposition_request(
        self,
        *,
        available_tools: tuple[ToolDescriptor, ...] = (),
        playbooks: tuple[PlaybookHint, ...] = (),
    ) -> GoalDecompositionRequest:
        with self._lock:
            return GoalDecompositionRequest(
                goal=self._goal.model_copy(deep=True),
                environment=self._environment.model_copy(deep=True),
                available_tools=available_tools,
                playbooks=playbooks,
                limits=self._limits,
            )

    def load_decomposition(self, proposal: DecompositionProposal) -> PlannerSnapshot:
        with self._lock:
            proposal = DecompositionProposal.model_validate(proposal.model_dump())
            if self._status is not GoalStatus.PLANNING:
                raise InvalidTransitionError("decomposition can only be loaded while planning")
            if proposal.goal_id != self._goal.goal_id:
                raise GraphValidationError("proposal goal_id does not match planner goal")
            if len(proposal.steps) > self._limits.max_steps:
                raise GraphValidationError("proposal exceeds the configured step limit")
            specs = {step.step_id: step for step in proposal.steps}
            order = _validate_graph(specs)
            self._specs = specs
            self._runtime = {step_id: _StepRuntime() for step_id in specs}
            self._topological_order = order
            self._status = GoalStatus.READY
            self._touch()
            return self._snapshot_locked()

    def ready_step_ids(self, environment: EnvironmentFingerprint | None = None) -> tuple[str, ...]:
        with self._lock:
            return self._ready_step_ids_locked(environment or self._environment)

    def reconcile_environment(self, environment: EnvironmentFingerprint) -> PlannerSnapshot:
        """Block or release dependency-ready steps after an environment change."""

        with self._lock:
            self._require_operational()
            environment = EnvironmentFingerprint.model_validate(environment.model_dump())
            self._environment = environment
            for step_id in self._topological_order:
                runtime = self._runtime[step_id]
                if runtime.status not in {StepStatus.PENDING, StepStatus.BLOCKED}:
                    continue
                dependencies_ready = all(
                    self._runtime[item].status is StepStatus.SUCCEEDED
                    for item in self._specs[step_id].dependencies
                )
                if not dependencies_ready:
                    continue
                matches = self._preconditions_match(self._specs[step_id], environment)
                if runtime.status is StepStatus.PENDING and not matches:
                    runtime.status = StepStatus.BLOCKED
                    runtime.last_error = "environment precondition fingerprint changed"
                elif runtime.status is StepStatus.BLOCKED and matches:
                    runtime.status = StepStatus.PENDING
                    runtime.last_error = None
            self._touch()
            return self._snapshot_locked()

    def start_step(
        self, step_id: str, *, environment: EnvironmentFingerprint | None = None
    ) -> StepSnapshot:
        with self._lock:
            self._require_operational()
            runtime = self._get_runtime(step_id)
            spec = self._specs[step_id]
            current = EnvironmentFingerprint.model_validate(
                (environment or self._environment).model_dump()
            )
            if runtime.status is StepStatus.BLOCKED and self._preconditions_match(spec, current):
                runtime.status = StepStatus.PENDING
                runtime.last_error = None
            if runtime.status is not StepStatus.PENDING:
                raise InvalidTransitionError(
                    f"step {step_id} cannot start while {runtime.status.value}"
                )
            unmet = [
                item
                for item in spec.dependencies
                if self._runtime[item].status is not StepStatus.SUCCEEDED
            ]
            if unmet:
                raise InvalidTransitionError(
                    f"step {step_id} has incomplete dependencies: {', '.join(unmet)}"
                )
            if not self._preconditions_match(spec, current):
                runtime.status = StepStatus.BLOCKED
                runtime.last_error = "environment precondition fingerprint changed"
                self._environment = current
                self._touch()
                raise PreconditionMismatchError(runtime.last_error)
            step_limit = self._step_attempt_limit(spec)
            if runtime.attempts >= step_limit:
                raise AttemptLimitError(f"step {step_id} exhausted its attempt limit")
            if self._total_attempts >= self._limits.max_total_attempts:
                raise AttemptLimitError("plan exhausted its total attempt limit")
            runtime.status = StepStatus.RUNNING
            runtime.attempts += 1
            runtime.started_at = _utc_now()
            runtime.started_environment_digest = current.digest
            runtime.finished_at = None
            runtime.action_evidence = {}
            runtime.bound_action = None
            runtime.verification_contract = None
            runtime.assertion_results = ()
            runtime.last_error = None
            self._total_attempts += 1
            self._environment = current
            if self._status is GoalStatus.READY:
                self._status = GoalStatus.RUNNING
            self._touch()
            return self._step_snapshot(step_id)

    def bind_verification_contract(
        self,
        step_id: str,
        contract: VerificationContract,
        *,
        action: ActionCall,
    ) -> StepSnapshot:
        """Bind the exact inspected action before it is allowed to execute."""

        with self._lock:
            runtime = self._get_runtime(step_id)
            if runtime.status is not StepStatus.RUNNING:
                raise InvalidTransitionError(
                    f"step {step_id} cannot bind an action while {runtime.status.value}"
                )
            contract = VerificationContract.model_validate(contract.model_dump())
            action = ActionCall.model_validate(action.model_dump())
            if (
                action.tool != contract.tool
                or _digest(action.arguments) != contract.arguments_sha256
            ):
                raise VerificationError("bound action does not match its exact contract")
            expected_objective = _digest(
                {
                    "step_id": step_id,
                    "objective": self._specs[step_id].objective,
                }
            )
            if contract.objective_sha256 != expected_objective:
                raise VerificationError("action contract does not match the step objective")
            runtime.bound_action = action
            runtime.verification_contract = contract
            self._touch()
            return self._step_snapshot(step_id)

    def begin_verification(self, step_id: str, *, action_evidence: dict[str, Any]) -> StepSnapshot:
        """Move a successful action into mandatory independent verification."""

        with self._lock:
            runtime = self._get_runtime(step_id)
            if runtime.status is not StepStatus.RUNNING:
                raise InvalidTransitionError(
                    f"step {step_id} cannot verify while {runtime.status.value}"
                )
            runtime.status = StepStatus.VERIFYING
            runtime.action_evidence = _json_object(action_evidence)
            self._touch()
            return self._step_snapshot(step_id)

    def record_verification(
        self, step_id: str, *, results: tuple[AssertionResult, ...]
    ) -> StepSnapshot:
        with self._lock:
            runtime = self._get_runtime(step_id)
            if runtime.status is not StepStatus.VERIFYING:
                raise InvalidTransitionError(
                    f"step {step_id} cannot complete while {runtime.status.value}"
                )
            spec = self._specs[step_id]
            results = tuple(
                AssertionResult.model_validate(result.model_dump()) for result in results
            )
            failed = self._validate_assertion_results(spec.criteria, results)
            runtime.assertion_results = tuple(
                result.model_copy(deep=True)
                for result in sorted(results, key=lambda result: result.assertion_id)
            )
            runtime.finished_at = _utc_now()
            if failed:
                runtime.status = StepStatus.FAILED
                runtime.last_error = "verification failed: " + ", ".join(failed)
            else:
                runtime.status = StepStatus.SUCCEEDED
                runtime.last_error = None
            self._refresh_goal_status()
            self._touch()
            return self._step_snapshot(step_id)

    def record_goal_verification(self, *, results: tuple[AssertionResult, ...]) -> PlannerSnapshot:
        """Apply independent verification to the goal-level definition of done."""

        with self._lock:
            if self._status is not GoalStatus.VERIFYING:
                raise InvalidTransitionError(
                    f"goal cannot complete verification while {self._status.value}"
                )
            results = tuple(
                AssertionResult.model_validate(result.model_dump()) for result in results
            )
            failed = self._validate_assertion_results(self._goal.criteria, results)
            self._goal_assertion_results = tuple(
                result.model_copy(deep=True)
                for result in sorted(results, key=lambda result: result.assertion_id)
            )
            if failed:
                # A failed final assertion remains revisable: a remediation branch
                # can be added without discarding successful work.
                self._status = GoalStatus.RUNNING
                self._failure_reason = "goal verification failed: " + ", ".join(failed)
            else:
                self._status = GoalStatus.SUCCEEDED
                self._failure_reason = None
            self._touch()
            return self._snapshot_locked()

    def fail_step(self, step_id: str, *, reason: str) -> StepSnapshot:
        with self._lock:
            runtime = self._get_runtime(step_id)
            if runtime.status not in {StepStatus.RUNNING, StepStatus.VERIFYING}:
                raise InvalidTransitionError(
                    f"step {step_id} cannot fail while {runtime.status.value}"
                )
            runtime.status = StepStatus.FAILED
            runtime.last_error = _bounded_reason(reason)
            runtime.finished_at = _utc_now()
            self._touch()
            return self._step_snapshot(step_id)

    def retry_step(self, step_id: str) -> StepSnapshot:
        with self._lock:
            self._require_operational()
            runtime = self._get_runtime(step_id)
            if runtime.status is not StepStatus.FAILED:
                raise InvalidTransitionError(
                    f"step {step_id} cannot retry while {runtime.status.value}"
                )
            limit = self._step_attempt_limit(self._specs[step_id])
            if runtime.attempts >= limit:
                raise AttemptLimitError(f"step {step_id} exhausted its attempt limit")
            if self._total_attempts >= self._limits.max_total_attempts:
                raise AttemptLimitError("plan exhausted its total attempt limit")
            runtime.status = StepStatus.PENDING
            runtime.started_at = None
            runtime.started_environment_digest = None
            runtime.finished_at = None
            runtime.action_evidence = {}
            runtime.bound_action = None
            runtime.verification_contract = None
            runtime.assertion_results = ()
            runtime.last_error = None
            self._touch()
            return self._step_snapshot(step_id)

    def apply_revision(self, revision: PlanRevision) -> PlannerSnapshot:
        """Atomically replace the unfinished portion of the graph.

        Successful steps are immutable and retain their runtime records.  Failed
        or blocked work may be removed/replaced, while explicit resets preserve
        the attempt counters so revision cannot bypass execution limits.
        """

        with self._lock:
            self._require_operational()
            revision = PlanRevision.model_validate(revision.model_dump())
            if revision.goal_id != self._goal.goal_id:
                raise RevisionConflictError("revision goal_id does not match planner goal")
            if revision.base_revision != self._revision:
                raise RevisionConflictError(
                    f"stale revision base {revision.base_revision}; current is {self._revision}"
                )
            if self._revision >= self._limits.max_revisions:
                raise AttemptLimitError("plan exhausted its graph revision limit")
            self._status = GoalStatus.REVISING
            try:
                specs = dict(self._specs)
                runtimes = dict(self._runtime)
                added_ids = {step.step_id for step in revision.add_steps}
                replaced_ids = {step.step_id for step in revision.replace_steps}
                removed_ids = set(revision.remove_step_ids)
                reset_ids = set(revision.reset_step_ids)
                if added_ids & set(specs):
                    raise RevisionConflictError("added step ids already exist")
                if replaced_ids - set(specs):
                    raise RevisionConflictError("replacement step ids do not exist")
                if removed_ids - set(specs):
                    raise RevisionConflictError("removed step ids do not exist")
                if reset_ids - (set(specs) | added_ids):
                    raise RevisionConflictError("reset step ids do not exist")
                protected = {
                    step_id
                    for step_id, runtime in runtimes.items()
                    if runtime.status
                    in {StepStatus.RUNNING, StepStatus.VERIFYING, StepStatus.SUCCEEDED}
                }
                changed = replaced_ids | removed_ids | reset_ids
                if protected & changed:
                    raise RevisionConflictError(
                        "active or successful steps cannot be replaced, removed, or reset"
                    )
                for step_id in removed_ids:
                    del specs[step_id]
                    del runtimes[step_id]
                for step in revision.replace_steps:
                    specs[step.step_id] = step
                for step in revision.add_steps:
                    specs[step.step_id] = step
                    runtimes[step.step_id] = _StepRuntime()
                if not specs:
                    raise GraphValidationError("a revision cannot remove every step")
                if len(specs) > self._limits.max_steps:
                    raise GraphValidationError("revision exceeds the configured step limit")
                order = _validate_graph(specs)
                for step_id in reset_ids:
                    runtime = runtimes[step_id]
                    runtime.status = StepStatus.PENDING
                    runtime.started_at = None
                    runtime.started_environment_digest = None
                    runtime.finished_at = None
                    runtime.action_evidence = {}
                    runtime.verification_contract = None
                    runtime.bound_action = None
                    runtime.assertion_results = ()
                    runtime.last_error = None
                next_revision = self._revision + 1
                record = RevisionSnapshot(
                    revision_id=revision.revision_id,
                    revision=next_revision,
                    reason=revision.reason,
                    environment_digest=revision.environment.digest,
                    applied_at=_utc_now(),
                    added=tuple(sorted(added_ids)),
                    replaced=tuple(sorted(replaced_ids)),
                    removed=tuple(sorted(removed_ids)),
                    reset=tuple(sorted(reset_ids)),
                )
                self._specs = specs
                self._runtime = runtimes
                self._topological_order = order
                self._environment = revision.environment
                self._revision = next_revision
                self._goal_assertion_results = ()
                self._revision_history.append(record)
                self._revision_history = self._revision_history[
                    -self._limits.max_revision_history :
                ]
                self._status = GoalStatus.RUNNING
                self._refresh_goal_status()
                self._touch()
                return self._snapshot_locked()
            except Exception:
                # No candidate collections are installed before all validation succeeds.
                self._status = GoalStatus.RUNNING if self._total_attempts else GoalStatus.READY
                self._touch()
                raise

    def fail_goal(self, reason: str) -> PlannerSnapshot:
        with self._lock:
            self._require_operational()
            if any(
                runtime.status in {StepStatus.RUNNING, StepStatus.VERIFYING}
                for runtime in self._runtime.values()
            ):
                raise InvalidTransitionError("cannot fail a goal while a step is active")
            self._status = GoalStatus.FAILED
            self._failure_reason = _bounded_reason(reason)
            self._touch()
            return self._snapshot_locked()

    def cancel(self, reason: str = "cancelled by operator") -> PlannerSnapshot:
        with self._lock:
            if self._status in {
                GoalStatus.SUCCEEDED,
                GoalStatus.FAILED,
                GoalStatus.CANCELLED,
            }:
                raise InvalidTransitionError(f"cannot cancel a {self._status.value} goal")
            finished_at = _utc_now()
            for runtime in self._runtime.values():
                if runtime.status is not StepStatus.SUCCEEDED:
                    runtime.status = StepStatus.CANCELLED
                    runtime.finished_at = finished_at
                    runtime.last_error = _bounded_reason(reason)
            self._status = GoalStatus.CANCELLED
            self._failure_reason = _bounded_reason(reason)
            self._touch()
            return self._snapshot_locked()

    def snapshot(self) -> PlannerSnapshot:
        with self._lock:
            return self._snapshot_locked()

    @classmethod
    def restore(
        cls,
        snapshot: PlannerSnapshot | dict[str, Any],
        *,
        limits: PlannerLimits | None = None,
        recover_inflight: bool = True,
        recover_step_ids: frozenset[str] | None = None,
    ) -> AdaptiveDAGPlanner:
        """Restore a snapshot, failing in-flight steps closed after a crash."""

        source = PlannerSnapshot.model_validate(
            snapshot.model_dump() if isinstance(snapshot, PlannerSnapshot) else snapshot
        )
        restored_limits = (
            source.limits if limits is None else _restrictive_limits(source.limits, limits)
        )
        planner = cls(
            goal=source.goal,
            environment=source.environment,
            limits=restored_limits,
        )
        with planner._lock:
            if not source.steps:
                if (
                    source.status is not GoalStatus.PLANNING
                    or source.topological_order
                    or source.ready_step_ids
                    or source.total_attempts
                    or source.retired_attempts
                    or source.revision
                    or source.revision_history
                ):
                    raise GraphValidationError("empty snapshot is not a clean planning state")
                planner._status = GoalStatus.PLANNING
                planner._updated_at = _utc_now()
                return planner
            if source.status is GoalStatus.PLANNING:
                raise GraphValidationError("planning snapshot cannot contain graph steps")
            if source.status is GoalStatus.REVISING:
                raise GraphValidationError("transient revising state cannot be restored")
            source_statuses = {item.status for item in source.steps}
            if source.status is GoalStatus.READY and (
                source.total_attempts or source_statuses - {StepStatus.PENDING, StepStatus.BLOCKED}
            ):
                raise GraphValidationError("ready snapshot contains executed work")
            if source.status in {GoalStatus.VERIFYING, GoalStatus.SUCCEEDED} and (
                source_statuses != {StepStatus.SUCCEEDED}
            ):
                raise GraphValidationError(
                    f"{source.status.value} snapshot contains unfinished steps"
                )
            if source.status in {
                GoalStatus.FAILED,
                GoalStatus.CANCELLED,
                GoalStatus.SUCCEEDED,
            } and source_statuses & {StepStatus.RUNNING, StepStatus.VERIFYING}:
                raise GraphValidationError("terminal snapshot contains active steps")
            specs = {item.spec.step_id: item.spec.model_copy(deep=True) for item in source.steps}
            order = _validate_graph(specs)
            if order != source.topological_order:
                raise GraphValidationError("snapshot topological order is not canonical")
            planner._specs = specs
            planner._topological_order = order
            planner._runtime = {}
            for item in source.steps:
                status = item.status
                error = item.last_error
                finished_at = item.finished_at
                recover_this_step = bool(
                    recover_inflight
                    and status in {StepStatus.RUNNING, StepStatus.VERIFYING}
                    and (recover_step_ids is None or item.spec.step_id in recover_step_ids)
                )
                if recover_this_step:
                    status = StepStatus.FAILED
                    error = "execution interrupted before durable completion"
                    finished_at = _utc_now()
                planner._runtime[item.spec.step_id] = _StepRuntime(
                    status=status,
                    attempts=item.attempts,
                    started_at=item.started_at,
                    started_environment_digest=item.started_environment_digest,
                    finished_at=finished_at,
                    action_evidence=_json_object(item.action_evidence),
                    bound_action=(
                        item.bound_action.model_copy(deep=True)
                        if item.bound_action is not None
                        else None
                    ),
                    verification_contract=(
                        item.verification_contract.model_copy(deep=True)
                        if item.verification_contract is not None
                        else None
                    ),
                    assertion_results=tuple(
                        result.model_copy(deep=True) for result in item.assertion_results
                    ),
                    last_error=error,
                )
            restored_attempts = sum(runtime.attempts for runtime in planner._runtime.values())
            if restored_attempts + source.retired_attempts != source.total_attempts:
                raise GraphValidationError("snapshot attempt total is inconsistent")
            if source.total_attempts > planner.limits.max_total_attempts:
                raise GraphValidationError("snapshot exceeds the total attempt limit")
            if any(item.attempts > planner._step_attempt_limit(item.spec) for item in source.steps):
                raise GraphValidationError("snapshot exceeds a step attempt limit")
            if source.revision > planner.limits.max_revisions:
                raise GraphValidationError("snapshot exceeds the configured revision limit")
            history_revisions = [item.revision for item in source.revision_history]
            if source.revision == 0 and history_revisions:
                raise GraphValidationError("unrevised snapshot contains revision history")
            if source.revision > 0 and (
                not history_revisions or history_revisions[-1] != source.revision
            ):
                raise GraphValidationError("snapshot revision history is incomplete")
            if any(
                current <= previous
                for previous, current in zip(history_revisions, history_revisions[1:], strict=False)
            ):
                raise GraphValidationError("snapshot revisions are not strictly ordered")
            if len(source.revision_history) > planner.limits.max_revision_history:
                raise GraphValidationError("snapshot exceeds the revision history limit")
            if len(specs) > planner.limits.max_steps:
                raise GraphValidationError("snapshot exceeds the configured step limit")
            planner._revision = source.revision
            planner._total_attempts = source.total_attempts
            planner._revision_history = [
                item.model_copy(deep=True) for item in source.revision_history
            ]
            planner._goal_assertion_results = tuple(
                item.model_copy(deep=True) for item in source.goal_assertion_results
            )
            planner._failure_reason = source.failure_reason
            planner._status = source.status
            if recover_inflight and any(
                item.status in {StepStatus.RUNNING, StepStatus.VERIFYING}
                and (recover_step_ids is None or item.spec.step_id in recover_step_ids)
                for item in source.steps
            ):
                planner._status = GoalStatus.RUNNING
            planner._updated_at = _utc_now()
            if planner._status is GoalStatus.SUCCEEDED:
                if not all(
                    runtime.status is StepStatus.SUCCEEDED for runtime in planner._runtime.values()
                ):
                    raise GraphValidationError("successful snapshot contains unfinished steps")
                try:
                    failed = planner._validate_assertion_results(
                        planner.goal.criteria, planner._goal_assertion_results
                    )
                except VerificationError as exc:
                    raise GraphValidationError(
                        "successful snapshot lacks passing goal assertions"
                    ) from exc
                if failed:
                    raise GraphValidationError("successful snapshot lacks passing goal assertions")
        return planner

    def _preconditions_match(self, spec: StepSpec, environment: EnvironmentFingerprint) -> bool:
        return all(item.matches(environment) for item in spec.preconditions)

    def _ready_step_ids_locked(self, environment: EnvironmentFingerprint) -> tuple[str, ...]:
        if self._status not in {GoalStatus.READY, GoalStatus.RUNNING}:
            return ()
        ready = []
        for step_id in self._topological_order:
            runtime = self._runtime[step_id]
            spec = self._specs[step_id]
            if runtime.status not in {StepStatus.PENDING, StepStatus.BLOCKED}:
                continue
            if runtime.status is StepStatus.BLOCKED and not self._preconditions_match(
                spec, environment
            ):
                continue
            if not all(
                self._runtime[item].status is StepStatus.SUCCEEDED for item in spec.dependencies
            ):
                continue
            if not self._preconditions_match(spec, environment):
                continue
            limit = self._step_attempt_limit(spec)
            if runtime.attempts < limit and self._total_attempts < self._limits.max_total_attempts:
                ready.append(step_id)
        return tuple(ready)

    def _step_snapshot(self, step_id: str) -> StepSnapshot:
        runtime = self._runtime[step_id]
        return StepSnapshot(
            spec=self._specs[step_id].model_copy(deep=True),
            status=runtime.status,
            attempts=runtime.attempts,
            started_at=runtime.started_at,
            started_environment_digest=runtime.started_environment_digest,
            finished_at=runtime.finished_at,
            action_evidence=_json_object(runtime.action_evidence),
            bound_action=(
                runtime.bound_action.model_copy(deep=True)
                if runtime.bound_action is not None
                else None
            ),
            verification_contract=(
                runtime.verification_contract.model_copy(deep=True)
                if runtime.verification_contract is not None
                else None
            ),
            assertion_results=tuple(
                item.model_copy(deep=True) for item in runtime.assertion_results
            ),
            last_error=runtime.last_error,
        )

    def _snapshot_locked(self) -> PlannerSnapshot:
        active_attempts = sum(runtime.attempts for runtime in self._runtime.values())
        return PlannerSnapshot(
            goal=self._goal.model_copy(deep=True),
            limits=self._limits,
            status=self._status,
            revision=self._revision,
            total_attempts=self._total_attempts,
            retired_attempts=self._total_attempts - active_attempts,
            environment=self._environment.model_copy(deep=True),
            steps=tuple(self._step_snapshot(step_id) for step_id in self._topological_order),
            topological_order=self._topological_order,
            ready_step_ids=self._ready_step_ids_locked(self._environment),
            goal_assertion_results=tuple(
                item.model_copy(deep=True) for item in self._goal_assertion_results
            ),
            revision_history=tuple(item.model_copy(deep=True) for item in self._revision_history),
            failure_reason=self._failure_reason,
            updated_at=self._updated_at,
        )

    def _get_runtime(self, step_id: str) -> _StepRuntime:
        try:
            return self._runtime[step_id]
        except KeyError as exc:
            raise GraphValidationError(f"unknown step id: {step_id}") from exc

    def _require_operational(self) -> None:
        if self._status not in {GoalStatus.READY, GoalStatus.RUNNING}:
            raise InvalidTransitionError(f"goal is not operational while {self._status.value}")

    def _step_attempt_limit(self, spec: StepSpec) -> int:
        requested = spec.max_attempts or self._limits.max_step_attempts
        return min(requested, self._limits.max_step_attempts)

    def _refresh_goal_status(self) -> None:
        if self._runtime and all(
            runtime.status is StepStatus.SUCCEEDED for runtime in self._runtime.values()
        ):
            self._status = GoalStatus.VERIFYING
            self._failure_reason = None

    @staticmethod
    def _validate_assertion_results(
        criteria_items: tuple[AssertionCriterion, ...],
        results: tuple[AssertionResult, ...],
    ) -> tuple[str, ...]:
        by_id = {result.assertion_id: result for result in results}
        if len(by_id) != len(results):
            raise VerificationError("assertion results contain duplicate ids")
        criteria = {criterion.assertion_id: criterion for criterion in criteria_items}
        unknown = sorted(set(by_id) - set(criteria))
        missing = sorted(
            criterion.assertion_id
            for criterion in criteria_items
            if criterion.required and criterion.assertion_id not in by_id
        )
        mismatched = sorted(
            assertion_id
            for assertion_id, result in by_id.items()
            if assertion_id in criteria and result.inspector != criteria[assertion_id].inspector
        )
        if unknown or missing or mismatched:
            details = []
            if unknown:
                details.append(f"unknown={','.join(unknown)}")
            if missing:
                details.append(f"missing={','.join(missing)}")
            if mismatched:
                details.append(f"inspector_mismatch={','.join(mismatched)}")
            raise VerificationError("invalid assertion results: " + "; ".join(details))
        return tuple(
            sorted(
                criterion.assertion_id
                for criterion in criteria_items
                if criterion.required and not by_id[criterion.assertion_id].passed
            )
        )

    def _touch(self) -> None:
        self._updated_at = _utc_now()


def _validate_graph(specs: dict[str, StepSpec]) -> tuple[str, ...]:
    if not specs:
        raise GraphValidationError("plan must contain at least one step")
    unknown = sorted(
        (step.step_id, dependency)
        for step in specs.values()
        for dependency in step.dependencies
        if dependency not in specs
    )
    if unknown:
        references = ", ".join(f"{step}->{dependency}" for step, dependency in unknown)
        raise GraphValidationError(f"plan contains unknown dependencies: {references}")
    indegree = {step_id: len(spec.dependencies) for step_id, spec in specs.items()}
    children: dict[str, list[str]] = {step_id: [] for step_id in specs}
    for step in specs.values():
        for dependency in step.dependencies:
            children[dependency].append(step.step_id)
    queue = [step_id for step_id, degree in indegree.items() if degree == 0]
    heapq.heapify(queue)
    order: list[str] = []
    while queue:
        step_id = heapq.heappop(queue)
        order.append(step_id)
        for child in sorted(children[step_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(queue, child)
    if len(order) != len(specs):
        cyclic = sorted(step_id for step_id, degree in indegree.items() if degree > 0)
        raise GraphValidationError("plan contains a cycle involving: " + ", ".join(cyclic))
    return tuple(order)


def _projected_digest(facts: dict[str, Any], paths: tuple[str, ...]) -> str:
    projection: dict[str, Any] = {}
    for path in sorted(paths):
        present, value = _resolve_pointer(facts, path)
        projection[path] = {"present": present, "value": value if present else None}
    return _digest(projection)


def _resolve_pointer(document: Any, pointer: str) -> tuple[bool, Any]:
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, _normalise_json(current)


def _require_unique(values: Any, label: str) -> None:
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError(f"{label} must be unique")


def _bounded_reason(reason: str) -> str:
    text = " ".join(reason.split())
    if not text:
        raise ValueError("reason cannot be blank")
    return text[:4000]


def _restrictive_limits(stored: PlannerLimits, requested: PlannerLimits) -> PlannerLimits:
    """Never let a restore override expand persisted safety/resource bounds."""

    return PlannerLimits(
        max_steps=min(stored.max_steps, requested.max_steps),
        max_revisions=min(stored.max_revisions, requested.max_revisions),
        max_total_attempts=min(stored.max_total_attempts, requested.max_total_attempts),
        max_step_attempts=min(stored.max_step_attempts, requested.max_step_attempts),
        max_revision_history=min(stored.max_revision_history, requested.max_revision_history),
    )


def _parse_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "PLANNER_PROTOCOL",
    "ActionCall",
    "AdaptiveDAGPlanner",
    "AssertionCriterion",
    "AssertionResult",
    "AttemptLimitError",
    "DecompositionProposal",
    "EnvironmentFingerprint",
    "GoalDecompositionRequest",
    "GoalDefinition",
    "GoalStatus",
    "GraphValidationError",
    "InvalidTransitionError",
    "PlanRevision",
    "PlannerError",
    "PlannerLimits",
    "PlannerSnapshot",
    "PlaybookHint",
    "PreconditionFingerprint",
    "PreconditionMismatchError",
    "RevisionConflictError",
    "RevisionSnapshot",
    "StepSnapshot",
    "StepSpec",
    "StepStatus",
    "ToolDescriptor",
    "VerificationError",
]
