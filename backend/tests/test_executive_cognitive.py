from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from jarvis_gpt.agent import AgentContext, AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.executive_runtime import (
    ExecutiveCoordinator,
    validate_mission_decomposition,
    validate_mission_goal_coverage,
)
from jarvis_gpt.llm import LLMResult
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


def _profile(digest: str) -> dict:
    return {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": digest * 64,
        "host": {
            "os": {"system": "Windows"},
            "architecture": {"machine": "AMD64"},
            "accelerators": {},
            "tools": {},
        },
    }


def _agent(monkeypatch, tmp_path, llm, *, profile: str = "a"):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setenv("JARVIS_VERIFY_ANSWERS", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    executive = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile(profile),
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=llm,
        bus=EventBus(),
        executive=executive,
    )
    return agent, storage


def test_llm_dag_and_cognitive_artifact_complete_without_self_claims(
    monkeypatch,
    tmp_path,
):
    proposal = {
        "protocol": "jarvis.mission-decomposition.v1",
        "steps": [
            {
                "step_id": "context",
                "title": "Inspect the deployment context",
                "objective": "Produce a bounded map of deployment constraints.",
                "dependencies": [],
                "assertion": "A context artifact is bound to this mission and step.",
            },
            {
                "step_id": "decision",
                "title": "Choose the deployment approach",
                "objective": "Record the selected approach and its acceptance checks.",
                "dependencies": ["context"],
                "assertion": "A decision artifact records an approach and acceptance checks.",
            },
        ],
        "rationale": "Inspect context before selecting an approach.",
    }

    class PlanningLLM:
        async def complete(self, messages, **_kwargs):
            if "mission-decomposition-v1" in messages[0]["content"]:
                return LLMResult(ok=True, content=json.dumps(proposal))
            return LLMResult(
                ok=True,
                content=(
                    "Recorded the deployment constraints, assumptions, and explicit "
                    "acceptance boundaries for the active mission step."
                ),
            )

    agent, storage = _agent(monkeypatch, tmp_path, PlanningLLM())
    mission = asyncio.run(
        agent.create_mission_planned("Select a deployment approach for the QA service")
    )
    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    plan = agent.executive.snapshot(mission["id"])["planner"]
    context = next(item for item in plan["steps"] if item["spec"]["step_id"] == "context")

    assert [item["title"] for item in mission["tasks"]] == [
        "Inspect the deployment context",
        "Choose the deployment approach",
    ]
    assert response.result.ok is True
    assert response.result.data["executive"]["verified"] is True
    assert context["status"] == "succeeded"
    assert (
        context["action_evidence"]["state_verification"]["inspector"]
        == "cognitive_artifact_inspector"
    )
    assert "verification" not in response.result.data
    storage.close()


def test_direct_read_only_tool_output_mints_typed_artifact_evidence(
    monkeypatch,
    tmp_path,
):
    class ReadOnlyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(
                    ok=True,
                    content='{"tool":"runtime.status","arguments":{}}',
                )
            return LLMResult(
                ok=True,
                content="The runtime status was inspected directly and recorded for the step.",
            )

    agent, storage = _agent(monkeypatch, tmp_path, ReadOnlyLLM())

    async def inspect_runtime(name, _arguments=None, **_kwargs):
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="Runtime is healthy.",
            data={"status": "healthy", "pid": 1234},
        )

    monkeypatch.setattr(agent.tools, "run", inspect_runtime)
    mission = agent.create_mission("Inspect runtime health and retain the evidence")
    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    plan = agent.executive.snapshot(mission["id"])["planner"]
    first = next(item for item in plan["steps"] if item["spec"]["step_id"] == "step.001")

    assert response.result.ok is True
    assert response.result.data["executive"]["verified"] is True
    assert (
        first["action_evidence"]["state_verification"]["inspector"]
        == "read_only_artifact_inspector"
    )
    observed = first["action_evidence"]["state_verification"]["evidence"][0]["observed"]
    assert observed["tool"] == "runtime.status"
    assert len(observed["output_sha256"]) == 64
    storage.close()


def test_unrelated_read_only_output_cannot_satisfy_step_assertion(
    monkeypatch,
    tmp_path,
):
    agent, storage = _agent(monkeypatch, tmp_path, object())
    mission = storage.create_mission(
        title="Production configuration",
        goal="Verify production configuration Orion",
        tasks=["Validate production configuration Orion constraints"],
    )
    agent.executive.create_for_mission(mission)
    claim = agent.executive.claim_ready_task(mission["id"])
    result = ToolRunResponse(
        tool="runtime.status",
        ok=True,
        summary="Runtime is healthy.",
        data={"status": "healthy", "pid": 1234},
    )

    evidence = agent.executive.capture_inspector_evidence(
        mission["id"],
        claim.task["id"],
        result,
        action_arguments={},
        read_only=True,
    )

    assert evidence.ok is False
    assert evidence.evidence[0]["observed"]["matched_terms"] == []
    storage.close()


def test_unrelated_cognitive_prose_cannot_satisfy_step_assertion(
    monkeypatch,
    tmp_path,
):
    agent, storage = _agent(monkeypatch, tmp_path, object())
    mission = storage.create_mission(
        title="Production configuration",
        goal="Implement production configuration Orion",
        tasks=["Implement production configuration Orion constraints"],
    )
    agent.executive.create_for_mission(mission)
    claim = agent.executive.claim_ready_task(mission["id"])
    summary = "Implemented and verified production configuration Orion constraints in full."
    summary_sha256 = hashlib.sha256(
        json.dumps(summary, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result = ToolRunResponse(
        tool="mission.execute_next",
        ok=True,
        summary=summary,
        data={
            "executive_artifact": {
                "protocol": "jarvis.cognitive-artifact.v1",
                "goal": mission["goal"],
                "task_title": claim.task["title"],
                "objective_sha256": "0" * 64,
                "assertion_sha256": "0" * 64,
                "summary_sha256": summary_sha256,
            }
        },
    )

    with pytest.raises(ValueError, match="artifact-only"):
        agent.executive.capture_cognitive_evidence(mission["id"], claim.task["id"], result)

    storage.close()


def test_empty_memory_search_echo_cannot_satisfy_observation_step(
    monkeypatch,
    tmp_path,
):
    agent, storage = _agent(monkeypatch, tmp_path, object())
    mission = storage.create_mission(
        title="Memory evidence",
        goal="Research production configuration Orion history",
        tasks=["Research production configuration Orion memory records"],
    )
    agent.executive.create_for_mission(mission)
    claim = agent.executive.claim_ready_task(mission["id"])
    result = ToolRunResponse(
        tool="memory.search",
        ok=True,
        summary="Memory search returned 0 item(s).",
        data={
            "items": [],
            "query": "production configuration Orion memory records",
            "limit": 10,
        },
    )

    evidence = agent.executive.capture_inspector_evidence(
        mission["id"],
        claim.task["id"],
        result,
        action_arguments={
            "query": "production configuration Orion memory records",
            "limit": 10,
        },
        read_only=True,
    )

    assert evidence.ok is False
    assert evidence.evidence[0]["observed"]["substantive_output"] is False
    storage.close()


def test_unrelated_typed_inspection_cannot_satisfy_observation_step(
    monkeypatch,
    tmp_path,
):
    agent, storage = _agent(monkeypatch, tmp_path, object())
    expected = tmp_path / "production-orion.json"
    unrelated = tmp_path / "unrelated.txt"
    mission = storage.create_mission(
        title="Configuration inspection",
        goal=f"Inspect production configuration at {expected}",
        tasks=[f"Inspect production configuration at {expected}"],
    )
    agent.executive.create_for_mission(mission)
    claim = agent.executive.claim_ready_task(mission["id"])
    result = ToolRunResponse(
        tool="execution.inspect",
        ok=True,
        summary="Production configuration inspection completed.",
        data={
            "result": {"summary": "Production configuration exists."},
            "verification": {
                "ok": True,
                "status": "passed",
                "action_id": "unrelated-stat",
                "action_kind": "StatPathAction",
                "summary": "Filesystem state matched.",
                "evidence": [
                    {
                        "source": "filesystem.stat",
                        "assertion": "path exists",
                        "expected": True,
                        "observed": True,
                        "passed": True,
                        "captured_at": "2026-07-11T00:00:00+00:00",
                        "error": None,
                        "subject": str(unrelated),
                    }
                ],
                "error": None,
            },
        },
    )

    evidence = agent.executive.capture_inspector_evidence(
        mission["id"],
        claim.task["id"],
        result,
        action_arguments={
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "kind": "fs.stat",
                    "action_id": "unrelated-stat",
                    "path": str(unrelated),
                },
            }
        },
        read_only=True,
    )

    assert evidence.ok is False
    scope = next(
        item for item in evidence.evidence if item["source"] == "executive.read_only_artifact"
    )
    assert scope["observed"]["inspection_subject_bound"] is False
    storage.close()


def test_llm_mission_adapts_to_changed_environment_and_completes_revalidation(
    monkeypatch,
    tmp_path,
):
    class CognitiveLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(
                    ok=True,
                    content='{"tool":"environment.profile","arguments":{}}',
                )
            return LLMResult(
                ok=True,
                content=(
                    "Revalidated the changed environment assumptions and recorded the "
                    "current tool availability for downstream work."
                ),
            )

    agent, storage = _agent(monkeypatch, tmp_path, CognitiveLLM(), profile="a")
    mission = agent.create_mission("Prepare a QA deployment plan")
    replacement = ExecutiveCoordinator(storage=storage, host_profile=_profile("b"))
    agent.executive = replacement
    agent.tools.executive = replacement

    async def inspect_environment(name, _arguments=None, **_kwargs):
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary=(
                "Changed host environment profile assumptions and tool availability "
                "were inspected."
            ),
            data={"profile": _profile("b")},
        )

    monkeypatch.setattr(agent.tools, "run", inspect_environment)

    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    plan = replacement.snapshot(mission["id"])["planner"]
    environment_steps = [
        item for item in plan["steps"] if item["spec"]["action"]["tool"] == "environment.profile"
    ]

    assert response.result.ok is True
    assert response.result.data["executive"]["verified"] is True
    assert plan["revision"] == 1
    assert len(environment_steps) == 1
    assert environment_steps[0]["status"] == "succeeded"
    storage.close()


def test_malformed_llm_decomposition_fails_before_mission_persistence(
    monkeypatch,
    tmp_path,
):
    class MalformedPlannerLLM:
        async def complete(self, _messages, **_kwargs):
            return LLMResult(
                ok=True,
                content=json.dumps(
                    {
                        "protocol": "jarvis.mission-decomposition.v1",
                        "steps": [
                            {
                                "step_id": "a",
                                "title": "A",
                                "objective": "First cyclic step",
                                "dependencies": ["b"],
                                "assertion": "A is evidenced",
                            },
                            {
                                "step_id": "b",
                                "title": "B",
                                "objective": "Second cyclic step",
                                "dependencies": ["a"],
                                "assertion": "B is evidenced",
                            },
                        ],
                        "rationale": "Invalid cyclic proposal",
                    }
                ),
            )

    agent, storage = _agent(monkeypatch, tmp_path, MalformedPlannerLLM())

    with pytest.raises(ValueError, match="acyclic"):
        asyncio.run(agent.create_mission_planned("Create a safe deployment plan"))

    assert storage.list_missions(limit=10) == []
    storage.close()


def test_state_goal_cannot_be_recast_as_artifact_only_decomposition(
    monkeypatch,
    tmp_path,
):
    proposal = {
        "protocol": "jarvis.mission-decomposition.v1",
        "steps": [
            {
                "step_id": "report",
                "title": "Produce an approach report",
                "objective": "Produce a report describing a possible approach.",
                "dependencies": [],
                "assertion": "A report artifact describes an approach.",
            },
            {
                "step_id": "summary",
                "title": "Summarize the report",
                "objective": "Record a concise summary of the report.",
                "dependencies": ["report"],
                "assertion": "A summary artifact exists.",
            },
        ],
        "rationale": "Return prose instead of changing state.",
    }

    class ArtifactOnlyPlannerLLM:
        async def complete(self, _messages, **_kwargs):
            return LLMResult(ok=True, content=json.dumps(proposal))

    agent, storage = _agent(monkeypatch, tmp_path, ArtifactOnlyPlannerLLM())

    with pytest.raises(ValueError, match="state mission goal"):
        asyncio.run(agent.create_mission_planned("Deploy Orion production service"))

    assert storage.list_missions(limit=10) == []
    storage.close()


def test_operator_mission_recovers_from_malformed_llm_decomposition(
    monkeypatch,
    tmp_path,
):
    """An operator's *own* explicit mission must still execute even when the LLM
    proposes a malformed decomposition.

    ``create_mission_planned`` keeps raising for every ordinary caller (the two
    tests above pin that contract), but the operator layer rebuilds the mission
    from the deterministic planner so an explicit command is never dropped just
    because the local model returned an incoherent DAG.
    """

    class MalformedPlannerLLM:
        async def complete(self, _messages, **_kwargs):
            return LLMResult(
                ok=True,
                content=json.dumps(
                    {
                        "protocol": "jarvis.mission-decomposition.v1",
                        "steps": [
                            {
                                "step_id": "a",
                                "title": "A",
                                "objective": "First cyclic step",
                                "dependencies": ["b"],
                                "assertion": "A is evidenced",
                            },
                            {
                                "step_id": "b",
                                "title": "B",
                                "objective": "Second cyclic step",
                                "dependencies": ["a"],
                                "assertion": "B is evidenced",
                            },
                        ],
                        "rationale": "Invalid cyclic proposal",
                    }
                ),
            )

    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "1")
    agent, storage = _agent(monkeypatch, tmp_path, MalformedPlannerLLM())

    goal = "Deploy Orion production service"
    context = AgentContext(
        conversation_id="conv-operator-mission",
        memory_hits=[],
        file_hits=[],
    )
    context.operator_request_digest = "req-digest-operator-mission"
    context.operator_message_id = "operator-msg-1"

    # The strict planner still rejects the cyclic DAG for ordinary callers.
    with pytest.raises(ValueError, match="acyclic"):
        asyncio.run(agent.create_mission_planned(goal))

    # The operator layer recovers and produces a persisted, goal-bound mission.
    mission = asyncio.run(agent._create_operator_mission_planned(goal, context))
    assert mission is not None
    assert mission["goal"] == goal
    assert len(mission["tasks"]) >= 2
    assert storage.get_mission(mission["id"]) is not None
    storage.close()


@pytest.mark.parametrize(
    ("goal", "policy"),
    [
        ("Create a PDF report", "state"),
        ("Create a Word document", "state"),
        ("Save the analysis to report.pdf", "state"),
        ("Создай отчет PDF", "state"),
        ("Audit production Orion security configuration", "observation"),
    ],
)
def test_authoritative_goal_policy_cannot_be_weakened_to_prose(goal, policy):
    decomposition = validate_mission_decomposition(
        {
            "protocol": "jarvis.mission-decomposition.v1",
            "steps": [
                {
                    "step_id": "design",
                    "title": "Design an approach plan",
                    "objective": "Produce a plan artifact describing an approach.",
                    "dependencies": [],
                    "assertion": "A plan artifact exists.",
                },
                {
                    "step_id": "summary",
                    "title": "Summarize the approach",
                    "objective": "Produce a summary artifact.",
                    "dependencies": ["design"],
                    "assertion": "A summary artifact exists.",
                },
            ],
            "rationale": "Artifact-only downgrade fixture.",
        }
    )

    with pytest.raises(ValueError, match=rf"{policy} mission goal"):
        validate_mission_goal_coverage(goal, decomposition)


def test_create_deployment_plan_remains_artifact_only_work():
    decomposition = validate_mission_decomposition(
        {
            "protocol": "jarvis.mission-decomposition.v1",
            "steps": [
                {
                    "step_id": "plan",
                    "title": "Create a deployment plan",
                    "objective": "Create a deployment plan and strategy artifact.",
                    "dependencies": [],
                    "assertion": "A deployment plan artifact exists.",
                },
                {
                    "step_id": "summary",
                    "title": "Summarize the deployment plan",
                    "objective": "Produce a concise deployment plan summary.",
                    "dependencies": ["plan"],
                    "assertion": "A summary artifact exists.",
                },
            ],
            "rationale": "Planning is a cognitive deliverable.",
        }
    )

    validated = validate_mission_goal_coverage("Create a deployment plan", decomposition)

    assert all(step.evidence_policy == "artifact" for step in validated.steps)
