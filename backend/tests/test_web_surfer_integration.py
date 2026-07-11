from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry
from jarvis_gpt.web_surfer_adapter import WebSurferAdapter


class _WebSurferService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fast_fact(self, query: str):
        self.calls.append(("fast_fact", query))
        return {"answer": f"fact:{query}"}

    async def deep_research(self, query: str):
        self.calls.append(("deep_research", query))
        return {"answer": f"research:{query}"}

    async def aggressive_shopping(self, product_url: str):
        self.calls.append(("aggressive_shopping", product_url))
        return [{"product": product_url, "price": 10, "currency": "USD"}]


class _FailedWebSurferService(_WebSurferService):
    async def fast_fact(self, query: str):
        return {"ok": False, "query": query, "error": "provider unavailable"}


def test_unprobed_or_missing_black_box_is_not_advertised_as_callable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    adapter = WebSurferAdapter(module_names=("missing_jarvis_web_surfer_test_module",))
    tools = ToolRegistry(
        settings,
        storage,
        LLMRouter(settings),
        web_surfer=adapter,
    )

    names = {item.name for item in tools.list()}
    capabilities = asyncio.run(tools.run("web.surfer.capabilities", {}))
    assert "web.surfer" not in names
    assert capabilities.ok is True
    assert capabilities.data["available"] is False
    assert capabilities.data["worker_pid"] is None
    storage.close()


def test_black_box_operation_failure_is_not_reported_as_tool_success(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(
        settings,
        storage,
        LLMRouter(settings),
        web_surfer=WebSurferAdapter(
            _FailedWebSurferService(), unsafe_in_process=True
        ),
    )

    result = asyncio.run(
        tools.run(
            "web.surfer",
            {"mode": "fast_fact", "arguments": {"query": "current fact"}},
        )
    )

    assert result.ok is False
    assert result.data["data"]["ok"] is False
    assert "provider unavailable" in result.summary
    storage.close()


def test_agent_routes_web_scenarios_only_through_public_black_box_methods(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = LLMRouter(settings)
    service = _WebSurferService()
    tools = ToolRegistry(
        settings,
        storage,
        llm,
        web_surfer=WebSurferAdapter(service, unsafe_in_process=True),
    )
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, tools=tools)

    fact = asyncio.run(
        agent._run_web_answer_engine(
            message="What is the current Python version?",
            query="current Python version",
            conversation_id=None,
        )
    )
    research = asyncio.run(
        agent._run_web_answer_engine(
            message="Compare and cross-check three database engines with sources",
            query="database engine comparison",
            conversation_id=None,
        )
    )
    shopping = asyncio.run(
        agent._run_web_answer_engine(
            message="Найди и сравни цены на RTX 5090",
            query="https://shop.example/rtx-5090",
            conversation_id=None,
        )
    )

    assert fact is not None and fact.answer == "fact:current Python version"
    assert research is not None and research.answer == "research:database engine comparison"
    assert shopping is not None and "https://shop.example/rtx-5090" in shopping.answer
    assert "price: 10" in shopping.answer
    assert service.calls == [
        ("fast_fact", "current Python version"),
        ("deep_research", "database engine comparison"),
        ("aggressive_shopping", "https://shop.example/rtx-5090"),
    ]
    storage.close()
