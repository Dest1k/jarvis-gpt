from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from jarvis_gpt.executive_planner import (
    ActionCall,
    AdaptiveDAGPlanner,
    AssertionCriterion,
    AssertionResult,
    AttemptLimitError,
    DecompositionProposal,
    EnvironmentFingerprint,
    GoalDefinition,
    GoalStatus,
    GraphValidationError,
    InvalidTransitionError,
    PlannerLimits,
    PlannerSnapshot,
    PlanRevision,
    PlaybookHint,
    PreconditionFingerprint,
    PreconditionMismatchError,
    RevisionConflictError,
    StepSpec,
    StepStatus,
    ToolDescriptor,
    VerificationContract,
    VerificationError,
)
from pydantic import ValidationError


def _criterion(
    assertion_id: str, *, inspector: str = "execution.inspect"
) -> AssertionCriterion:
    return AssertionCriterion(
        assertion_id=assertion_id,
        description=f"verify {assertion_id}",
        inspector=inspector,
        expected={"ok": True},
    )


def _goal() -> GoalDefinition:
    return GoalDefinition(
        goal_id="goal.test",
        objective="Produce a verified result",
        criteria=(_criterion("goal.done", inspector="goal.inspect"),),
    )


def _environment(version: str = "1", *, port: int = 9000) -> EnvironmentFingerprint:
    return EnvironmentFingerprint.capture(
        {
            "host": {"os": "windows", "version": version},
            "service": {"port": port},
        }
    )


def _step(
    step_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    preconditions: tuple[PreconditionFingerprint, ...] = (),
    max_attempts: int | None = None,
) -> StepSpec:
    return StepSpec(
        step_id=step_id,
        title=f"Step {step_id}",
        objective=f"Complete {step_id}",
        action=ActionCall(tool="execution.apply", arguments={"id": step_id}),
        dependencies=dependencies,
        criteria=(_criterion(f"{step_id}.verified"),),
        preconditions=preconditions,
        max_attempts=max_attempts,
    )


def _proposal(*steps: StepSpec) -> DecompositionProposal:
    return DecompositionProposal(goal_id="goal.test", steps=steps)


def _result(
    assertion_id: str,
    *,
    passed: bool = True,
    inspector: str = "execution.inspect",
) -> AssertionResult:
    return AssertionResult(
        assertion_id=assertion_id,
        inspector=inspector,
        passed=passed,
        evidence={"observed": passed},
    )


def _finish_step(
    planner: AdaptiveDAGPlanner, step_id: str, *, passed: bool = True
) -> None:
    planner.start_step(step_id)
    planner.begin_verification(step_id, action_evidence={"exit_code": 0})
    planner.record_verification(
        step_id,
        results=(_result(f"{step_id}.verified", passed=passed),),
    )


def _finish_goal(planner: AdaptiveDAGPlanner, *, passed: bool = True) -> PlannerSnapshot:
    return planner.record_goal_verification(
        results=(
            _result("goal.done", passed=passed, inspector="goal.inspect"),
        )
    )


def test_decomposition_contract_is_strict_versioned_and_contains_context() -> None:
    environment = _environment()
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=environment)

    request = planner.decomposition_request(
        available_tools=(
            ToolDescriptor(
                name="execution.apply",
                input_schema_sha256="a" * 64,
            ),
        ),
        playbooks=(
            PlaybookHint(
                playbook_id="playbook.1",
                symptom="service will not start",
                solution="repair configuration",
                verification="probe the socket",
            ),
        ),
    )

    assert request.protocol == "jarvis.planner.v1"
    assert request.environment.digest == environment.digest
    assert request.available_tools[0].name == "execution.apply"
    assert request.playbooks[0].verification == "probe the socket"
    assert request.model_json_schema()["additionalProperties"] is False
    with pytest.raises(ValidationError):
        request.__class__.model_validate({**request.model_dump(), "unknown": True})


def test_environment_fingerprint_rejects_tampering_and_non_json_values() -> None:
    environment = _environment()

    with pytest.raises(ValidationError, match="digest"):
        EnvironmentFingerprint(
            digest="0" * 64,
            captured_at=environment.captured_at,
            facts=environment.facts,
        )
    with pytest.raises((ValidationError, ValueError), match="finite"):
        EnvironmentFingerprint.capture({"bad": float("nan")})


def test_planner_detaches_mutable_json_from_inputs_and_snapshots() -> None:
    environment = _environment()
    step = _step("alpha")
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=environment)
    planner.load_decomposition(_proposal(step))

    environment.facts["host"]["version"] = "tampered"
    step.action.arguments["id"] = "tampered"
    exposed = planner.snapshot()
    exposed.environment.facts["host"]["version"] = "also-tampered"
    exposed.steps[0].spec.action.arguments["id"] = "also-tampered"

    clean = planner.snapshot()
    assert clean.environment.facts["host"]["version"] == "1"
    assert clean.steps[0].spec.action.arguments["id"] == "alpha"


def test_topological_order_and_parallel_ready_selection_are_deterministic() -> None:
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())

    snapshot = planner.load_decomposition(
        _proposal(
            _step("zeta"),
            _step("merge", dependencies=("zeta", "alpha")),
            _step("alpha"),
        )
    )

    assert snapshot.topological_order == ("alpha", "zeta", "merge")
    assert planner.ready_step_ids() == ("alpha", "zeta")
    _finish_step(planner, "zeta")
    assert planner.ready_step_ids() == ("alpha",)
    _finish_step(planner, "alpha")
    assert planner.ready_step_ids() == ("merge",)


def test_graph_rejects_unknown_dependencies_and_cycles_without_partial_load() -> None:
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())
    with pytest.raises(GraphValidationError, match="unknown"):
        planner.load_decomposition(_proposal(_step("alpha", dependencies=("missing",))))
    assert planner.status is GoalStatus.PLANNING

    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())
    with pytest.raises(GraphValidationError, match="cycle"):
        planner.load_decomposition(
            _proposal(
                _step("alpha", dependencies=("beta",)),
                _step("beta", dependencies=("alpha",)),
            )
        )
    assert planner.status is GoalStatus.PLANNING


def test_step_success_requires_action_and_independent_assertion_verification() -> None:
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())
    planner.load_decomposition(_proposal(_step("alpha")))

    started = planner.start_step("alpha")
    assert started.started_environment_digest == planner.snapshot().environment.digest
    with pytest.raises(InvalidTransitionError):
        planner.record_verification(
            "alpha", results=(_result("alpha.verified"),)
        )
    verifying = planner.begin_verification(
        "alpha", action_evidence={"reported_ok": True}
    )
    assert verifying.status is StepStatus.VERIFYING
    with pytest.raises(VerificationError, match="missing"):
        planner.record_verification("alpha", results=())
    with pytest.raises(VerificationError, match="inspector_mismatch"):
        planner.record_verification(
            "alpha",
            results=(
                _result("alpha.verified", inspector="untrusted.action.result"),
            ),
        )

    done = planner.record_verification(
        "alpha", results=(_result("alpha.verified"),)
    )
    assert done.status is StepStatus.SUCCEEDED
    assert planner.status is GoalStatus.VERIFYING


def test_goal_assertions_gate_success_and_failed_check_allows_remediation() -> None:
    environment = _environment()
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=environment)
    planner.load_decomposition(_proposal(_step("alpha")))
    _finish_step(planner, "alpha")

    with pytest.raises(VerificationError, match="missing"):
        planner.record_goal_verification(results=())
    failed = _finish_goal(planner, passed=False)
    assert failed.status is GoalStatus.RUNNING
    assert failed.failure_reason == "goal verification failed: goal.done"

    revised = planner.apply_revision(
        PlanRevision(
            revision_id="revision.remediate",
            goal_id="goal.test",
            base_revision=0,
            reason="final socket assertion failed",
            environment=environment,
            add_steps=(_step("remediate", dependencies=("alpha",)),),
        )
    )
    alpha = next(item for item in revised.steps if item.spec.step_id == "alpha")
    assert alpha.status is StepStatus.SUCCEEDED
    assert alpha.attempts == 1
    _finish_step(planner, "remediate")
    completed = _finish_goal(planner)
    assert completed.status is GoalStatus.SUCCEEDED


def test_precondition_fingerprints_block_changed_environment_and_recover() -> None:
    original = _environment("1")
    changed = _environment("2")
    precondition = PreconditionFingerprint.from_environment(
        name="host.version",
        environment=original,
        fact_paths=("/host/version",),
    )
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=original)
    planner.load_decomposition(
        _proposal(_step("alpha", preconditions=(precondition,)))
    )

    with pytest.raises(PreconditionMismatchError):
        planner.start_step("alpha", environment=changed)
    blocked = planner.snapshot().steps[0]
    assert blocked.status is StepStatus.BLOCKED
    assert planner.ready_step_ids(changed) == ()

    released = planner.reconcile_environment(original)
    assert released.steps[0].status is StepStatus.PENDING
    assert released.ready_step_ids == ("alpha",)


def test_revision_preserves_completed_state_and_replaces_failed_branch() -> None:
    environment = _environment()
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=environment)
    planner.load_decomposition(
        _proposal(_step("prepare"), _step("deploy", dependencies=("prepare",)))
    )
    _finish_step(planner, "prepare")
    planner.start_step("deploy")
    planner.fail_step("deploy", reason="environment changed")

    snapshot = planner.apply_revision(
        PlanRevision(
            revision_id="revision.1",
            goal_id="goal.test",
            base_revision=0,
            reason="switch to an environment-compatible deployment path",
            environment=environment,
            remove_step_ids=("deploy",),
            add_steps=(_step("deploy.safe", dependencies=("prepare",)),),
        )
    )

    assert snapshot.revision == 1
    assert snapshot.topological_order == ("prepare", "deploy.safe")
    prepare = next(item for item in snapshot.steps if item.spec.step_id == "prepare")
    assert prepare.status is StepStatus.SUCCEEDED
    assert prepare.attempts == 1
    assert snapshot.ready_step_ids == ("deploy.safe",)
    assert snapshot.revision_history[0].removed == ("deploy",)


def test_revision_reset_clears_bound_action_and_contract_together() -> None:
    environment = _environment()
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=environment)
    planner.load_decomposition(_proposal(_step("alpha")))
    planner.start_step("alpha")
    action = ActionCall(tool="execution.apply", arguments={"id": "alpha"}, destructive=True)

    def digest(value) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    planner.bind_verification_contract(
        "alpha",
        VerificationContract(
            tool="execution.apply",
            arguments_sha256=digest(action.arguments),
            action_id="alpha-action",
            action_kind="WriteFileAction",
            postcondition_sha256="a" * 64,
            objective_sha256=digest(
                {"step_id": "alpha", "objective": "Complete alpha"}
            ),
        ),
        action=action,
    )
    planner.fail_step("alpha", reason="retry with a revised precondition")
    snapshot = planner.apply_revision(
        PlanRevision(
            revision_id="revision.reset-bound",
            goal_id="goal.test",
            base_revision=0,
            reason="reset failed step",
            environment=environment,
            reset_step_ids=("alpha",),
        )
    )
    step = snapshot.steps[0]
    assert step.status is StepStatus.PENDING
    assert step.bound_action is None
    assert step.verification_contract is None


def test_revision_is_optimistic_atomic_cycle_safe_and_protects_success() -> None:
    environment = _environment()
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=environment)
    planner.load_decomposition(_proposal(_step("alpha"), _step("beta")))

    cyclic = PlanRevision(
        revision_id="revision.cycle",
        goal_id="goal.test",
        base_revision=0,
        reason="invalid proposal",
        environment=environment,
        replace_steps=(
            _step("alpha", dependencies=("beta",)),
            _step("beta", dependencies=("alpha",)),
        ),
    )
    before = planner.snapshot().model_dump(mode="json", exclude={"updated_at"})
    with pytest.raises(GraphValidationError, match="cycle"):
        planner.apply_revision(cyclic)
    after = planner.snapshot().model_dump(mode="json", exclude={"updated_at"})
    assert after == before

    with pytest.raises(RevisionConflictError, match="stale"):
        planner.apply_revision(cyclic.model_copy(update={"base_revision": 1}))

    _finish_step(planner, "alpha")
    with pytest.raises(RevisionConflictError, match="successful"):
        planner.apply_revision(
            PlanRevision(
                revision_id="revision.overwrite",
                goal_id="goal.test",
                base_revision=0,
                reason="must not overwrite completed work",
                environment=environment,
                replace_steps=(_step("alpha"),),
            )
        )


def test_attempt_and_revision_limits_cannot_be_bypassed() -> None:
    environment = _environment()
    planner = AdaptiveDAGPlanner(
        goal=_goal(),
        environment=environment,
        limits=PlannerLimits(
            max_steps=4,
            max_revisions=0,
            max_total_attempts=1,
            max_step_attempts=1,
        ),
    )
    planner.load_decomposition(_proposal(_step("alpha")))
    planner.start_step("alpha")
    planner.fail_step("alpha", reason="failed")

    with pytest.raises(AttemptLimitError, match="attempt"):
        planner.retry_step("alpha")
    with pytest.raises(AttemptLimitError, match="revision"):
        planner.apply_revision(
            PlanRevision(
                revision_id="revision.forbidden",
                goal_id="goal.test",
                base_revision=0,
                reason="limit reached",
                environment=environment,
                remove_step_ids=("alpha",),
                add_steps=(_step("beta"),),
            )
        )


def test_step_proposal_cannot_raise_the_global_attempt_cap() -> None:
    planner = AdaptiveDAGPlanner(
        goal=_goal(),
        environment=_environment(),
        limits=PlannerLimits(max_total_attempts=5, max_step_attempts=1),
    )
    planner.load_decomposition(_proposal(_step("alpha", max_attempts=32)))
    planner.start_step("alpha")
    planner.fail_step("alpha", reason="failed")

    with pytest.raises(AttemptLimitError, match="attempt"):
        planner.retry_step("alpha")

    restored = AdaptiveDAGPlanner.restore(
        planner.snapshot().model_dump(mode="json"),
        limits=PlannerLimits(max_total_attempts=50, max_step_attempts=32),
    )
    assert restored.snapshot().limits.max_step_attempts == 1
    with pytest.raises(AttemptLimitError, match="attempt"):
        restored.retry_step("alpha")


def test_snapshot_round_trip_recovers_inflight_step_as_failed() -> None:
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())
    planner.load_decomposition(_proposal(_step("alpha", max_attempts=2)))
    planner.start_step("alpha")
    payload = deepcopy(planner.snapshot().model_dump(mode="json"))

    restored = AdaptiveDAGPlanner.restore(payload)
    recovered = restored.snapshot()

    assert recovered.status is GoalStatus.RUNNING
    assert recovered.steps[0].status is StepStatus.FAILED
    assert recovered.steps[0].attempts == 1
    assert "interrupted" in (recovered.steps[0].last_error or "")
    assert restored.retry_step("alpha").status is StepStatus.PENDING


def test_planning_and_revised_snapshots_round_trip() -> None:
    environment = _environment()
    planning = AdaptiveDAGPlanner(goal=_goal(), environment=environment)
    restored_planning = AdaptiveDAGPlanner.restore(
        planning.snapshot().model_dump(mode="json")
    )
    assert restored_planning.status is GoalStatus.PLANNING

    planner = AdaptiveDAGPlanner(goal=_goal(), environment=environment)
    planner.load_decomposition(_proposal(_step("old")))
    planner.start_step("old")
    planner.fail_step("old", reason="replace it")
    revised = planner.apply_revision(
        PlanRevision(
            revision_id="revision.replace",
            goal_id="goal.test",
            base_revision=0,
            reason="use another branch",
            environment=environment,
            remove_step_ids=("old",),
            add_steps=(_step("new"),),
        )
    )
    assert revised.retired_attempts == 1

    restored = AdaptiveDAGPlanner.restore(revised.model_dump(mode="json"))
    assert restored.snapshot().retired_attempts == 1
    assert restored.snapshot().total_attempts == 1
    assert restored.ready_step_ids() == ("new",)


def test_snapshot_rejects_false_success_without_goal_verification() -> None:
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())
    planner.load_decomposition(_proposal(_step("alpha")))
    _finish_step(planner, "alpha")
    payload = planner.snapshot().model_dump(mode="json")
    payload["status"] = "succeeded"

    with pytest.raises((GraphValidationError, VerificationError), match="goal assertions"):
        AdaptiveDAGPlanner.restore(payload)


def test_successful_snapshot_round_trip_retains_goal_assertions() -> None:
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())
    planner.load_decomposition(_proposal(_step("alpha")))
    _finish_step(planner, "alpha")
    completed = _finish_goal(planner)

    restored = AdaptiveDAGPlanner.restore(completed.model_dump(mode="json"))

    assert restored.status is GoalStatus.SUCCEEDED
    assert restored.snapshot().goal_assertion_results[0].assertion_id == "goal.done"


def test_cancellation_is_terminal_and_preserves_successful_steps() -> None:
    planner = AdaptiveDAGPlanner(goal=_goal(), environment=_environment())
    planner.load_decomposition(_proposal(_step("alpha"), _step("beta")))
    _finish_step(planner, "alpha")

    snapshot = planner.cancel("operator stopped task")

    by_id = {item.spec.step_id: item for item in snapshot.steps}
    assert snapshot.status is GoalStatus.CANCELLED
    assert by_id["alpha"].status is StepStatus.SUCCEEDED
    assert by_id["beta"].status is StepStatus.CANCELLED
    assert snapshot.ready_step_ids == ()
    with pytest.raises(InvalidTransitionError):
        planner.start_step("beta")
