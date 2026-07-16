"""Live-data intent routing: shopping/price/travel queries must reach the web path.

Regression guard for the misroute where "where is a 5090 cheapest" was classified as
`document_memory` (because persisted files existed and the query mentioned "сейчас"),
tried documents.recall, and dead-ended asking for a file name instead of searching
the web.
"""

from __future__ import annotations

from jarvis_gpt.agent import AgentContext, AgentRuntime, _looks_like_live_web_query
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage


def _plan_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    # Simulate a runtime that already has persisted files — the exact condition that
    # made the temporal-reference heuristic hijack live-web queries into recall.
    monkeypatch.setattr(storage, "list_files", lambda *a, **k: [{"id": "f1"}])
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus()
    )
    return agent, storage


def _route(agent, message: str):
    ctx = AgentContext(conversation_id="c", memory_hits=[], file_hits=[])
    return agent._plan_task(message, ctx, mode="auto", attachments=[])


def test_live_web_query_helper_detects_purchase_and_travel():
    assert _looks_like_live_web_query("где дешевле всего купить rtx 5090")
    assert _looks_like_live_web_query("сколько стоит билет на поезд до Казани")
    assert _looks_like_live_web_query(
        "посчитай стоимость поездки в екатеринбург, учитывая реальные билеты"
    )
    assert _looks_like_live_web_query("в каком магазине заказать это дешевле")
    assert not _looks_like_live_web_query(
        "прочитай мой сохранённый документ про архитектуру и сделай выжимку"
    )
    assert not _looks_like_live_web_query("объясни разницу между TCP и UDP")


def test_shopping_query_routes_to_web_even_with_persisted_files(monkeypatch, tmp_path):
    agent, storage = _plan_agent(monkeypatch, tmp_path)
    plan = _route(
        agent,
        "Где сейчас в России дешевле всего купить видеокарту RTX 5090? "
        "Сравни несколько магазинов и назови самый дешёвый вариант с ценой и ссылкой.",
    )
    assert plan.route == "web_research"
    assert plan.intent == "shopping_research"
    assert "web.search" in plan.tools
    storage.close()


def test_travel_cost_query_routes_to_web(monkeypatch, tmp_path):
    agent, storage = _plan_agent(monkeypatch, tmp_path)
    plan = _route(
        agent,
        "посчитай стоимость поездки в екатеринбург из донецка через неделю, "
        "учитывая реальные билеты",
    )
    assert plan.route == "web_research"
    assert "web.search" in plan.tools
    storage.close()


def test_document_query_still_routes_to_document_memory(monkeypatch, tmp_path):
    agent, storage = _plan_agent(monkeypatch, tmp_path)
    plan = _route(agent, "прочитай мой сохранённый документ про архитектуру и сделай выжимку")
    assert plan.intent == "document_memory"
    storage.close()
