"""End-to-end smoke test of the FastAPI surface through the real ASGI app.

Every other test exercises a component in isolation; nothing drove the routes
through the app the way the Command Center does. A wrong response_model, a
missing await, or broken route wiring would ship silently. This test boots the
real app (offline LLM, autonomy off) and walks the critical operator journey:
status -> chat -> feedback -> mission -> report -> queue -> tools/memory/approvals.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from threading import Event

import pytest
from fastapi import FastAPI
from jarvis_gpt.api import (
    _is_local_machine_host,
    _is_loopback_host,
    _origin_allowed,
    _persist_interrupted_stream,
    _token_allowed,
    app,
    lifespan,
)
from jarvis_gpt.config import load_settings
from jarvis_gpt.model_hub import DOWNLOAD_JOBS_KEY
from jarvis_gpt.runtime_lease import PrimaryRuntimeLease, RuntimeLeaseError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    with TestClient(app) as test_client:
        yield test_client


def test_user_visible_answer_helper_blocks_tool_envelopes():
    """SPARK-0006: API-facing helper must scrub call:/tool envelopes from finals."""
    from jarvis_gpt.agent import TOOL_PROTOCOL_FAILURE_ANSWER, _user_visible_answer

    for payload in (
        "call:documents.read",
        "call:llm.health",
        'call:dispatcher.status\n{"tool":"dispatcher.status","arguments":{}}',
    ):
        visible = _user_visible_answer(payload)
        assert "call:" not in visible.lower()
        assert '"tool"' not in visible
        assert visible == TOOL_PROTOCOL_FAILURE_ANSWER


def test_lifespan_cleanup_survives_repeated_cancellation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": "a" * 64,
        "host": {},
    }
    monkeypatch.setattr(
        "jarvis_gpt.api.HostProfileManager.refresh", lambda _self: profile
    )

    async def no_start(_self):
        return None

    monkeypatch.setattr("jarvis_gpt.api.RuntimeSupervisor.start", no_start)

    async def scenario():
        isolated_app = FastAPI()
        context = lifespan(isolated_app)
        await context.__aenter__()
        stop_started = asyncio.Event()
        release_stop = asyncio.Event()

        async def delayed_stop():
            stop_started.set()
            await release_stop.wait()

        isolated_app.state.supervisor.stop = delayed_stop
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await stop_started.wait()
        closing.cancel()
        closing.cancel()
        closing.cancel()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(closing), timeout=0.05)
        finally:
            release_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await closing

        lease = PrimaryRuntimeLease(
            isolated_app.state.settings.state_dir / "primary-runtime.lock"
        )
        lease.acquire()
        lease.release()

    asyncio.run(scenario())


def test_lifespan_holds_primary_lease_until_cancelled_host_refresh_finishes(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    refresh_started = Event()
    release_refresh = Event()

    def delayed_refresh(_self):
        refresh_started.set()
        assert release_refresh.wait(timeout=5)
        return {
            "schema": "jarvis.host-profile.v1",
            "fingerprint_sha256": "b" * 64,
            "host": {},
        }

    monkeypatch.setattr("jarvis_gpt.api.HostProfileManager.refresh", delayed_refresh)
    lease_path = load_settings().state_dir / "primary-runtime.lock"

    async def scenario():
        isolated_app = FastAPI()
        context = lifespan(isolated_app)
        startup = asyncio.create_task(context.__aenter__())
        assert await asyncio.to_thread(refresh_started.wait, 2)
        startup.cancel()
        startup.cancel()
        await asyncio.sleep(0)
        lease = PrimaryRuntimeLease(lease_path)
        with pytest.raises(RuntimeLeaseError):
            lease.acquire()
        assert not startup.done()
        release_refresh.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
        lease.acquire()
        lease.release()

    asyncio.run(scenario())


def test_health_and_status(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    status = client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert "settings" in body
    assert "counters" in body

    models = client.get("/api/models")
    assert models.status_code == 200

    profile = client.get("/api/environment/profile")
    assert profile.status_code == 200
    assert len(profile.json()["profile"]["fingerprint_sha256"]) == 64
    assert (app.state.settings.home / "host_profile.json").exists()

    surfer = client.get("/api/internet/web-surfer")
    assert surfer.status_code == 200
    assert surfer.json()["protocol"] == "jarvis.web-surfer-adapter.v1"


def test_read_only_web_status_endpoints_do_not_create_tool_runs(client):
    before = len(app.state.storage.list_tool_runs(limit=200))

    handoff = client.get("/api/browser/handoff")
    observability = client.get("/api/internet/observability?limit=20")

    after = len(app.state.storage.list_tool_runs(limit=200))
    assert handoff.status_code == 200
    assert handoff.json() is None
    assert observability.status_code == 200
    assert "summary" in observability.json()
    assert after == before


def test_model_download_can_be_cancelled(client):
    job = {
        "id": "modeldl_test",
        "repo_id": "owner/model",
        "target": str(app.state.settings.model_root / "owner__model"),
        "status": "queued",
        "summary": "Queued model download.",
    }
    app.state.storage.set_runtime_value(DOWNLOAD_JOBS_KEY, [job])

    response = client.post(f"/api/model-hub/downloads/{job['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert app.state.model_hub.download_jobs()[0]["status"] == "cancelled"


def test_api_cannot_reapprove_an_executing_approval(client):
    approval = app.state.storage.create_approval(
        title="No replay",
        description="Execution claim must be one-way.",
        requested_action="memory.save",
        payload={"content": "once"},
    )
    app.state.storage.update_approval(approval["id"], status="approved")
    claimed = app.state.storage.claim_approval_execution(approval["id"])

    response = client.patch(
        f"/api/approvals/{approval['id']}",
        json={"status": "approved", "result": {}},
    )

    assert claimed is not None
    assert response.status_code == 409
    assert app.state.storage.get_approval(approval["id"])["status"] == "executing"


def test_primary_runtime_recovers_interrupted_approval_without_replay(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    with TestClient(app):
        mission = app.state.storage.create_mission(
            title="Interrupted approval",
            goal="Do not replay acquired work",
            tasks=["Persist memory"],
        )
        task = mission["tasks"][0]
        app.state.storage.update_mission_task(
            task["id"], mission_id=mission["id"], status="blocked"
        )
        approval = app.state.storage.create_approval(
            title="Acquired before shutdown",
            description="Simulated abrupt process exit.",
            requested_action="memory.save",
            payload={
                "mission_id": mission["id"],
                "task_id": task["id"],
                "content": "interrupted side effect must not replay",
            },
        )
        app.state.storage.update_approval(approval["id"], status="approved")
        claimed = app.state.storage.claim_approval_execution(approval["id"])
        assert claimed is not None and claimed["status"] == "executing"

    with TestClient(app):
        recovered = app.state.storage.get_approval(approval["id"])
        assert recovered is not None and recovered["status"] == "failed"
        assert recovered["result"]["data"]["reconcile_only"] is True
        assert recovered["result"]["reconciliation"]["status"] == "completed"
        assert (
            app.state.storage.search_memory(
                "interrupted side effect must not replay", limit=5
            )
            == []
        )


def test_runtime_security_and_backup(client, monkeypatch):
    security = client.get("/api/runtime/security")
    assert security.status_code == 200
    assert security.json()["remote_requires_token"] is True
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("10.0.0.50") is False
    monkeypatch.setattr(
        "jarvis_gpt.api._local_interface_addresses",
        lambda: frozenset({"10.0.0.50"}),
    )
    assert _is_local_machine_host("10.0.0.50") is True
    assert _is_local_machine_host("10.0.0.51") is False
    monkeypatch.setenv("JARVIS_API_TOKEN", "secret")
    assert _token_allowed("secret") is True
    assert _token_allowed("wrong") is False

    backup = client.post("/api/runtime/backup")
    assert backup.status_code == 200
    body = backup.json()
    assert body["ok"] is True
    assert Path(body["path"]).exists()


def test_local_api_rejects_cross_site_state_change(client, monkeypatch):
    called = False

    def forbidden_compose(_action):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(app.state.dispatcher, "run_compose", forbidden_compose)

    denied = client.post(
        "/api/dispatcher/stop",
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert denied.status_code == 403
    assert called is False


def test_dispatcher_action_api_preserves_independent_verification(client, monkeypatch):
    status = app.state.dispatcher.status()
    monkeypatch.setattr(app.state.dispatcher, "status", lambda: status)
    monkeypatch.setattr(
        app.state.dispatcher,
        "run_compose_verified",
        lambda _action: {
            "ok": True,
            "summary": "verified",
            "returncode": 0,
            "verification": {
                "ok": True,
                "container_known": True,
                "container_running": True,
                "port_open": True,
            },
        },
    )

    response = client.post("/api/dispatcher/start")

    assert response.status_code == 200
    assert response.json()["verification"]["ok"] is True
    assert response.json()["verification"]["port_open"] is True


def test_loopback_api_can_require_token(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    monkeypatch.setenv("JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK", "1")
    monkeypatch.setenv("JARVIS_API_TOKEN", "secret")

    with TestClient(app) as test_client:
        denied = test_client.get("/api/status")
        allowed = test_client.get(
            "/api/status",
            headers={"X-Jarvis-Api-Token": "secret"},
        )
        security = test_client.get(
            "/api/runtime/security",
            headers={"X-Jarvis-Api-Token": "secret"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert security.json()["loopback_requires_token"] is True


def _websocket_token_protocol(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
    return f"jarvis.token.{encoded}"


def _assert_websocket_denied(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    subprotocols: list[str] | None = None,
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect(
            path,
            headers=headers,
            subprotocols=subprotocols,
        ),
    ):
        raise AssertionError("WebSocket unexpectedly accepted invalid authentication")
    assert caught.value.code == 1008


def test_websocket_enforces_strict_token_and_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    monkeypatch.setenv("JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK", "1")
    monkeypatch.setenv("JARVIS_API_TOKEN", "secret")
    monkeypatch.setenv("JARVIS_CORS_ORIGINS", "http://192.168.50.4:3000")
    protocol = _websocket_token_protocol("secret")

    with TestClient(app) as test_client:
        _assert_websocket_denied(
            test_client,
            "/ws/events",
            headers={"origin": "http://localhost:3000"},
        )
        # Long-lived secrets are intentionally not accepted in URLs.
        _assert_websocket_denied(
            test_client,
            "/ws/events?token=secret",
            headers={"origin": "http://localhost:3000"},
        )
        _assert_websocket_denied(
            test_client,
            "/ws/events",
            headers={"origin": "https://example.invalid"},
            subprotocols=[protocol],
        )

        with test_client.websocket_connect(
            "/ws/events",
            headers={"origin": "http://192.168.50.4:3000"},
            subprotocols=[protocol],
        ) as websocket:
            assert websocket is not None

    assert _origin_allowed("http://localhost:3000") is True
    assert _origin_allowed("http://192.168.50.4:3000/") is True
    assert _origin_allowed("https://example.invalid") is False


def test_operator_quality_and_autonomy_cancel(client):
    quality = client.get("/api/operator/quality")
    assert quality.status_code == 200
    assert "negative_feedback" in quality.json()

    created = client.post(
        "/api/autonomy/jobs",
        json={
            "title": "Cancelable diagnostics",
            "kind": "diagnostics",
            "cadence": "1m",
            "priority": 42,
            "budget": {"max_runs": 3, "max_minutes": 5},
        },
    )
    assert created.status_code == 200
    job = created.json()
    assert job["priority"] == 42

    cancelled = client.post(f"/api/autonomy/jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_autonomy_start_runs_detached(client, monkeypatch):
    created = client.post(
        "/api/autonomy/jobs",
        json={
            "title": "Detached diagnostics",
            "kind": "diagnostics",
            "cadence": "manual",
            "budget": {"max_runs": 3, "max_minutes": 5},
        },
    )
    assert created.status_code == 200
    job = created.json()
    entered = Event()

    async def fake_run_job(started_job):
        entered.set()
        return {"job": started_job, "ok": True, "summary": "ok", "data": {}}

    monkeypatch.setattr(app.state.autonomy_executor, "run_job", fake_run_job)

    started = client.post(f"/api/autonomy/jobs/{job['id']}/start")

    assert started.status_code == 200
    assert started.json()["id"] == job["id"]
    assert entered.wait(1)


def test_cors_is_loopback_only(client):
    requested = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    allowed = client.options(
        "/api/chat",
        headers={**requested, "Origin": "http://localhost:3000"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"

    denied = client.options(
        "/api/chat",
        headers={**requested, "Origin": "https://example.invalid"},
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_chat_offline_and_feedback_roundtrip(client):
    response = client.post(
        "/api/chat",
        json={"message": "Привет, JARVIS", "mode": "chat"},
    )
    assert response.status_code == 200
    payload = response.json()
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    assert payload["answer"]
    assert message_id.startswith("msg_")

    # The conversation and its messages are retrievable.
    conversations = client.get("/api/conversations")
    assert conversations.status_code == 200
    assert any(item["id"] == conversation_id for item in conversations.json())

    messages = client.get(f"/api/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    assert any(item["id"] == message_id for item in messages.json())

    # Operator feedback persists in the message metadata.
    feedback = client.post(
        f"/api/messages/{message_id}/feedback",
        json={"rating": "down", "comment": "нужно короче"},
    )
    assert feedback.status_code == 200
    assert feedback.json()["metadata"]["feedback"]["rating"] == "down"

    missing = client.post("/api/messages/msg_nope/feedback", json={"rating": "up"})
    assert missing.status_code == 404

    bad_rating = client.post(
        f"/api/messages/{message_id}/feedback",
        json={"rating": "meh"},
    )
    assert bad_rating.status_code == 422


def test_interrupted_stream_partial_answer_is_recoverable(client):
    conversation_id = app.state.storage.create_conversation("interrupted stream")
    saved = _persist_interrupted_stream(
        app.state.storage,
        conversation_id=conversation_id,
        partial=["partial ", "answer"],
        events=[{"type": "assistant_done", "payload": {"source": "test"}}],
    )

    response = client.get(f"/api/chat/stream/interrupted/{conversation_id}")
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()

    assert saved is not None
    assert response.status_code == 200
    assert response.json()["message_id"] == saved["message_id"]
    assert messages[-1]["content"] == "partial answer"
    assert messages[-1]["metadata"]["interrupted"] is True


def test_mission_lifecycle_and_report(client):
    created = client.post(
        "/api/missions",
        json={"goal": "Проверить свободное место на дисках и отчитаться"},
    )
    assert created.status_code == 200
    mission_id = created.json()["id"]
    assert created.json()["tasks"]
    plan = client.get(f"/api/executive/plans/{mission_id}")
    assert plan.status_code == 200
    assert plan.json()["protocol"] == "jarvis.executive.v1"
    assert plan.json()["planner"]["ready_step_ids"] == ["step.001"]
    bypass = client.patch(
        f"/api/missions/{mission_id}/tasks/{created.json()['tasks'][0]['id']}",
        json={"status": "done"},
    )
    assert bypass.status_code == 409

    # No report before the mission is finished.
    early = client.get(f"/api/missions/{mission_id}/report")
    assert early.status_code == 404

    # Offline mode must fail closed instead of fabricating successful evidence.
    run = client.post(f"/api/missions/{mission_id}/run")
    assert run.status_code == 200
    run_body = run.json()
    assert run_body["completed"] is False
    assert run_body["stopped_reason"] == "blocked"
    assert run_body["final_report"] is None

    # No report or successful goal assertion exists without trusted execution evidence.
    report = client.get(f"/api/missions/{mission_id}/report")
    assert report.status_code == 404

    final_plan = client.get(f"/api/executive/plans/{mission_id}").json()
    assert final_plan["planner"]["status"] in {"ready", "running"}
    assert final_plan["planner"]["goal_assertion_results"] == []

    playbooks = client.get(
        "/api/memory/playbooks",
        params={"query": "свободное место диск", "limit": 5},
    )
    assert playbooks.status_code == 200
    # Offline/LLM mission reports are deliverables, not trusted typed-action lessons.
    assert playbooks.json()["stats"]["entries"] == 0

    missing = client.get("/api/missions/mission_nope/report")
    assert missing.status_code == 404


def test_rejected_mission_approval_reconciles_executive_branch(client):
    created = client.post(
        "/api/missions",
        json={"goal": "Exercise rejection recovery"},
    ).json()
    mission_id = created["id"]
    claim = app.state.executive.claim_ready_task(mission_id)
    assert claim is not None
    app.state.storage.update_mission_task(
        claim.task["id"],
        mission_id=mission_id,
        status="blocked",
        notes="Waiting for approval.",
    )
    step = next(
        item
        for item in claim.planner["steps"]
        if item["spec"]["step_id"] == claim.step_id
    )
    approval = client.post(
        "/api/approvals",
        json={
            "title": "Bound approval",
            "description": "Reject and revise the branch.",
            "requested_action": "tool.run",
            "risk": "danger",
            "payload": {
                "mission_id": mission_id,
                "task_id": claim.task["id"],
                "tool": "diagnostics.run",
                "arguments": {},
                "executive_claim": {
                    "protocol": "jarvis.executive-approval.v1",
                    "mission_id": mission_id,
                    "task_id": claim.task["id"],
                    "step_id": claim.step_id,
                    "plan_revision": claim.planner["revision"],
                    "step_attempt": step["attempts"],
                    "environment_digest": claim.planner["environment"]["digest"],
                },
            },
        },
    ).json()

    rejected = client.patch(
        f"/api/approvals/{approval['id']}",
        json={"status": "rejected", "result": {"operator": "test"}},
    )

    assert rejected.status_code == 200
    assert rejected.json()["result"]["reconciliation"]["status"] == "completed"
    plan = client.get(f"/api/executive/plans/{mission_id}").json()
    assert plan["planner"]["revision"] == 1
    old_task = next(
        item
        for item in app.state.storage.list_mission_tasks(mission_id)
        if item["id"] == claim.task["id"]
    )
    assert old_task["status"] == "skipped"


def test_operator_queue_and_memory_and_tools(client):
    queue = client.get("/api/operator/queue")
    assert queue.status_code == 200
    assert "items" in queue.json()
    assert "summary" in queue.json()

    memory = client.post(
        "/api/memory",
        json={
            "content": "Оператор держит рантайм Jarvis в D:/jarvis.",
            "namespace": "environment",
            "tags": ["operator"],
            "importance": 0.7,
        },
    )
    assert memory.status_code == 200
    hits = client.get("/api/memory", params={"q": "рантайм", "limit": 5})
    assert hits.status_code == 200
    assert any("Jarvis" in item["content"] for item in hits.json())

    tools = client.get("/api/tools")
    assert tools.status_code == 200
    names = {tool["name"] for tool in tools.json()}
    assert {"runtime.status", "persona.get", "memory.search"}.issubset(names)
    assert "host.bridge.execute" not in names
    assert "execution.apply" in names
    assert {
        "execution.preflight",
        "environment.profile",
        "memory.playbooks.lookup",
        "executive.plan.status",
        "web.surfer.capabilities",
    }.issubset(names)

    run = client.post("/api/tools/runtime.status/run", json={"arguments": {}})
    assert run.status_code == 200
    assert run.json()["ok"] is True

    approvals_before = len(app.state.storage.list_approvals(limit=200))
    raw_target = app.state.settings.data_dir / "raw-command-should-not-run.txt"
    raw_command = f"Set-Content -LiteralPath '{raw_target}' -Value raw"

    # Removed raw-command tools stay unavailable even with a danger override.
    denied_raw = client.post(
        "/api/tools/host.bridge.execute/run",
        json={"arguments": {"command": raw_command}, "allow_danger": False},
    )
    bypass_raw = client.post(
        "/api/tools/host.bridge.execute/run",
        json={"arguments": {"command": raw_command}, "allow_danger": True},
    )

    assert denied_raw.status_code == 200
    assert bypass_raw.status_code == 200
    assert denied_raw.json()["ok"] is False
    assert bypass_raw.json()["ok"] is False
    assert "not registered" in denied_raw.json()["summary"]
    assert "not registered" in bypass_raw.json()["summary"]
    assert not raw_target.exists()
    assert len(app.state.storage.list_approvals(limit=200)) == approvals_before

    structured_target = app.state.settings.data_dir / "structured-gated.txt"
    structured_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": "api-gated-write",
            "path": str(structured_target),
            "content_base64": base64.b64encode(b"gated").decode("ascii"),
        },
    }
    gated = client.post(
        "/api/tools/execution.apply/run",
        json={"arguments": {"payload": structured_payload}, "allow_danger": False},
    )
    assert gated.status_code == 200
    assert gated.json()["ok"] is False
    assert gated.json()["data"]["danger_level"] == "danger"
    assert gated.json()["data"]["approval_action"] == "tool.run"
    assert gated.json()["data"]["approval_payload"]["tool"] == "execution.apply"
    assert not structured_target.exists()


def test_approvals_and_persona(client):
    approval = client.post(
        "/api/approvals",
        json={
            "title": "Проверка допуска",
            "description": "smoke",
            "requested_action": "tool.run",
            "risk": "review",
            "payload": {"tool": "runtime.status", "arguments": {}},
        },
    )
    assert approval.status_code == 200
    approval_id = approval.json()["id"]
    listed = client.get("/api/approvals", params={"status": "pending"})
    assert listed.status_code == 200
    assert any(item["id"] == approval_id for item in listed.json())

    persona = client.get("/api/persona")
    assert persona.status_code == 200
    patched = client.patch("/api/persona", json={"location": "Казань"})
    assert patched.status_code == 200
    assert patched.json()["location"] == "Казань"
    assert client.get("/api/persona").json()["location"] == "Казань"
