"""Service mode and model overload notices."""

from __future__ import annotations

from pathlib import Path

from jarvis_gpt.runtime_notices import (
    blocking_notice,
    collect_notices,
    force_overload,
    get_service_mode,
    record_llm_sample,
    set_service_mode,
    user_facing_reply,
)


def test_service_mode_roundtrip(tmp_path: Path):
    state = set_service_mode(
        tmp_path, enabled=True, message="Плановое обновление", until="2099-01-01T00:00:00+00:00"
    )
    assert state["enabled"] is True
    mode = get_service_mode(tmp_path)
    assert mode["enabled"] is True
    notices = collect_notices(tmp_path)
    assert any(item["kind"] == "service_mode" for item in notices)
    block = blocking_notice(tmp_path)
    assert block is not None
    assert "техн" in user_facing_reply(block).lower() or "работ" in user_facing_reply(block).lower()
    set_service_mode(tmp_path, enabled=False)
    assert get_service_mode(tmp_path)["enabled"] is False


def test_service_mode_expires(tmp_path: Path):
    set_service_mode(
        tmp_path, enabled=True, message="done", until="2000-01-01T00:00:00+00:00"
    )
    assert get_service_mode(tmp_path)["enabled"] is False


def test_overload_flag():
    force_overload(False)
    force_overload(True, reason="latency")
    notices = collect_notices(Path("."))  # overload is process-global, not file-based
    assert any(item["kind"] == "model_overload" and item["active"] for item in notices)
    force_overload(False)


def test_record_llm_samples_can_trip_overload():
    force_overload(False)
    for _ in range(6):
        record_llm_sample(error=True)
    state_notices = collect_notices(Path("."))
    assert any(item["kind"] == "model_overload" for item in state_notices)
    force_overload(False)
