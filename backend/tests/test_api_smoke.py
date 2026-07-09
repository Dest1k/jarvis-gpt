"""End-to-end smoke test of the FastAPI surface through the real ASGI app.

Every other test exercises a component in isolation; nothing drove the routes
through the app the way the Command Center does. A wrong response_model, a
missing await, or broken route wiring would ship silently. This test boots the
real app (offline LLM, autonomy off) and walks the critical operator journey:
status -> chat -> feedback -> mission -> report -> queue -> tools/memory/approvals.
"""

from __future__ import annotations

import pytest
from jarvis_gpt.api import app
from starlette.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    with TestClient(app) as test_client:
        yield test_client


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


def test_mission_lifecycle_and_report(client):
    created = client.post(
        "/api/missions",
        json={"goal": "Проверить свободное место на дисках и отчитаться"},
    )
    assert created.status_code == 200
    mission_id = created.json()["id"]
    assert created.json()["tasks"]

    # No report before the mission is finished.
    early = client.get(f"/api/missions/{mission_id}/report")
    assert early.status_code == 404

    # Drive the mission to completion (offline deterministic executor).
    run = client.post(f"/api/missions/{mission_id}/run")
    assert run.status_code == 200
    run_body = run.json()
    assert run_body["completed"] is True
    assert run_body["final_report"]
    assert "Итог миссии" in run_body["final_report"]

    # The finished report is now retrievable and stable.
    report = client.get(f"/api/missions/{mission_id}/report")
    assert report.status_code == 200
    assert report.json()["report"] == run_body["final_report"]

    missing = client.get("/api/missions/mission_nope/report")
    assert missing.status_code == 404


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

    run = client.post("/api/tools/runtime.status/run", json={"arguments": {}})
    assert run.status_code == 200
    assert run.json()["ok"] is True

    # A dangerous tool without approval must be refused, not executed.
    denied = client.post(
        "/api/tools/host.bridge.execute/run",
        json={"arguments": {"command": "Get-Date"}, "allow_danger": False},
    )
    assert denied.status_code == 200
    assert denied.json()["ok"] is False


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
