"""Chat routing of shop-specific price queries to web.shop_search (not web.answer)."""

from __future__ import annotations

import asyncio

from jarvis_gpt.agent import (
    AgentRuntime,
    _format_shop_search_answer,
    _shop_key_from_message,
    _shop_search_url_for,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


def _agent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus()
    )
    return agent, storage


_RANKED = {
    "ok": True,
    "shop": "dns",
    "city": "Москва",
    "count": 2,
    "cheapest": {
        "title": "Palit RTX 5090 GameRock OC",
        "url": "https://www.dns-shop.ru/product/def/",
        "price_text": "409 999 ₽",
        "price_value": 409999.0,
    },
    "items": [
        {
            "title": "Palit RTX 5090 GameRock OC",
            "url": "https://www.dns-shop.ru/product/def/",
            "price_text": "409 999 ₽",
            "price_value": 409999.0,
        },
        {
            "title": "ASUS RTX 5090 ROG Astral",
            "url": "https://www.dns-shop.ru/product/ghi/",
            "price_text": "499 999 ₽",
            "price_value": 499999.0,
        },
    ],
}


def test_shop_key_and_helpers():
    assert _shop_key_from_message("найди самую дешёвую 5090 на днс") == "dns"
    assert _shop_key_from_message("сколько стоит rtx 5090 на ozon") == "ozon"
    assert _shop_key_from_message("какая погода в казани") is None
    assert _shop_search_url_for("dns", "rtx 5090").startswith("https://www.dns-shop.ru/search/?q=")
    assert _shop_search_url_for("unknown", "x") == ""


def test_format_shop_search_answer_lists_cheapest_first():
    answer = _format_shop_search_answer(_RANKED, "rtx 5090")
    assert "Самая дешёвая" in answer
    assert "409 999 ₽" in answer
    assert "Все варианты по возрастанию цены" in answer
    assert "для города: Москва" in answer
    # cheapest appears before the pricier card
    assert answer.index("409 999") < answer.index("499 999")


def test_shopping_dns_query_routes_to_shop_search_not_web_answer(monkeypatch, tmp_path):
    # The routing hook is gated on the browser layer being installed; force it
    # available so the test exercises the routing regardless of the CI env.
    monkeypatch.setattr("jarvis_gpt.agent._web_surfer_available", lambda: True)
    agent, storage = _agent(monkeypatch, tmp_path)
    called: list[str] = []

    async def fake_run(name, arguments=None, **kwargs):
        called.append(name)
        if name == "web.shop_search":
            return ToolRunResponse(
                tool="web.shop_search", ok=True, summary="2 товара", data=_RANKED
            )
        raise AssertionError(f"web.shop_search must win; got call to {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_web_research(
            "Найди мне самую дешёвую 5090 на днс",
            "самая дешёвая rtx 5090 dns",
            conversation_id=storage.create_conversation("shop"),
        )
    )

    assert "web.shop_search" in called
    assert "web.answer" not in called  # the fallback engine was not reached
    assert "409 999 ₽" in action.answer
    assert "Самая дешёвая" in action.answer
    storage.close()


def test_shop_search_needs_install_gives_actionable_message(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)

    async def fake_run(name, arguments=None, **kwargs):
        assert name == "web.shop_search"
        return ToolRunResponse(
            tool="web.shop_search",
            ok=False,
            summary="Browser surfer is unavailable",
            data={"needs_install": True},
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_shop_search("найди дешёвую 5090 на днс", "dns", conversation_id=None)
    )
    assert action is not None
    assert "playwright install chromium" in action.answer
    assert "requirements-surfer.txt" in action.answer
    # honest actionable message, not the misleading "site returned no data"
    assert "dns-shop.ru/search" in action.answer
    storage.close()


def test_shop_search_soft_failure_stays_honest_and_does_not_fall_back(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        assert name == "web.shop_search"
        captured.update(arguments or {})
        return ToolRunResponse(
            tool="web.shop_search",
            ok=False,
            summary="anti-bot wall",
            data={"error": "anti-bot"},
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_shop_search("найди дешёвую 5090 на днс", "dns", conversation_id=None)
    )
    assert captured == {"query": "rtx 5090", "shop": "dns"}
    assert "anti-bot" in action.answer
    assert "не подменяю результат общим веб-поиском" in action.answer
    assert "dns-shop.ru/search" in action.answer
    storage.close()
