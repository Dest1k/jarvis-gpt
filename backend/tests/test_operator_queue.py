from __future__ import annotations

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.operator_queue import (
    memory_hygiene_report,
    model_profile_plan,
    operator_queue_snapshot,
)
from jarvis_gpt.storage import JarvisStorage


def test_operator_queue_collects_approvals_missions_and_future_profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    mission = storage.create_mission(
        title="Host check",
        goal="Run approved host check",
        tasks=["Get host date"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    approval = storage.create_approval(
        title="Run host command",
        description="Needs operator approval",
        requested_action="tool.run",
        risk="danger",
        payload={"tool": "host.bridge.execute", "mission_id": mission["id"], "task_id": task["id"]},
    )
    storage.record_health(component="dispatcher", status="warn", message="warming up")

    queue = operator_queue_snapshot(settings, storage)

    ids = {item["id"] for item in queue["items"]}
    assert f"approval:{approval['id']}" in ids
    assert f"mission:{mission['id']}:{task['id']}" in ids
    assert "health:dispatcher" in ids
    assert "model:future-profiles" in ids
    assert queue["context"]["pending_approvals"] == 1
    storage.close()


def test_memory_hygiene_and_model_profile_scaffold(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.add_memory(
        content="Same durable fact",
        namespace="operator",
        tags=["operator"],
        importance=0.6,
    )
    storage.add_memory(
        content="Same durable fact",
        namespace="operator",
        tags=["operator"],
        importance=0.6,
    )
    storage.add_memory(
        content="Low confidence draft",
        namespace="operator",
        tags=["draft"],
        importance=0.2,
    )

    hygiene = memory_hygiene_report(storage)
    profiles = model_profile_plan(settings)

    assert hygiene["stats"]["total"] >= 2
    assert hygiene["stats"]["low_confidence"] >= 1
    assert any(item["status"] == "future" for item in profiles["profiles"])
    storage.close()
