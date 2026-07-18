"""Live-data intent routing: shopping/price/travel queries must reach the web path.

Regression guard for the misroute where "where is a 5090 cheapest" was classified as
`document_memory` (because persisted files existed and the query mentioned "сейчас"),
tried documents.recall, and dead-ended asking for a file name instead of searching
the web.
"""

from __future__ import annotations

from jarvis_gpt.agent import (
    AgentContext,
    AgentRuntime,
    _looks_like_filesystem_search,
    _looks_like_live_web_query,
    _looks_like_raw_tool_echo,
    _request_needs_web_lookup,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage


def _plan_agent(monkeypatch, tmp_path, *, autonomy: bool = False):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "1" if autonomy else "0")
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


def test_filesystem_search_predicate():
    # Concrete on-disk searches (verb + drive path or explicit disk/folder scope).
    assert _looks_like_filesystem_search("найди слово TODO в папке D:\\jarvis-gpt\\backend")
    assert _looks_like_filesystem_search("search for TODO on disk")
    assert _looks_like_filesystem_search("поищи config.py в директории проекта")
    # NOT filesystem searches — must not steal these.
    assert not _looks_like_filesystem_search(
        "прочитай мой сохранённый документ про архитектуру"
    )
    assert not _looks_like_filesystem_search(
        "найди в загруженных документах упоминание про архитектуру"
    )
    assert not _looks_like_filesystem_search("в каких файлах упоминается ковид")
    assert not _looks_like_filesystem_search("найди в каталоге ноутбук подешевле")
    assert not _looks_like_filesystem_search("где дешевле всего купить rtx 5090")


def test_filesystem_search_routes_to_filesystem_find(monkeypatch, tmp_path):
    agent, storage = _plan_agent(monkeypatch, tmp_path)
    plan = _route(agent, "найди слово TODO в папке D:\\jarvis-gpt\\backend")
    assert plan.route == "local_action"
    assert plan.intent == "filesystem.find"
    assert plan.tools == ("filesystem.find",)
    storage.close()


def test_filesystem_route_does_not_steal_document_recall(monkeypatch, tmp_path):
    # A "найди" with no drive path / disk marker must stay eligible for document recall,
    # never be hijacked into the filesystem.find route.
    agent, storage = _plan_agent(monkeypatch, tmp_path)
    plan = _route(agent, "найди в загруженных документах упоминание про архитектуру")
    assert plan.intent != "filesystem.find"
    storage.close()


def test_raw_tool_echo_detects_filesystem_find_dump():
    dump = (
        '{"root": "D:/x", "matches": [{"path": "a.py", "line": 3}], '
        '"truncated": false, "files_scanned": 12}'
    )
    assert _looks_like_raw_tool_echo(dump)
    assert not _looks_like_raw_tool_echo("Нашёл 1 совпадение в файле a.py на строке 3.")


def test_needs_web_lookup_helper():
    assert _request_needs_web_lookup("узнай последнюю версию Node.js и сохрани в файл")
    assert _request_needs_web_lookup("найди актуальную цену и запиши")
    assert not _request_needs_web_lookup("сделай отчёт по этим данным")
    assert not _request_needs_web_lookup("напиши стихотворение про осень")


def test_under_specified_artifact_does_not_clarify_under_autonomy(monkeypatch, tmp_path):
    agent, storage = _plan_agent(monkeypatch, tmp_path, autonomy=True)
    plan = _route(agent, "Сделай markdown-файл с планом на неделю из 5 пунктов.")
    assert plan.intent != "clarification"
    assert plan.needs_clarification is False
    storage.close()


def test_lookup_and_save_is_not_sealed_to_one_shot_generation(monkeypatch, tmp_path):
    agent, storage = _plan_agent(monkeypatch, tmp_path, autonomy=True)
    plan = _route(
        agent, "Узнай последнюю LTS-версию Node.js и сохрани её в node-version.md."
    )
    # Must research before creating the artifact, not seal straight to generation.
    assert plan.tools != ("documents.generate",)
    assert plan.intent != "new_artifact"
    storage.close()


def test_format_filesystem_find_answer_lists_files_not_counts():
    from jarvis_gpt.agent import _format_filesystem_find_answer

    disp = r"D:\jarvis-gpt\backend\dispatcher.py"
    ag = r"D:\jarvis-gpt\backend\agent.py"
    data = {
        "root": r"D:\jarvis-gpt\backend",
        "files_scanned": 40,
        "truncated": True,
        "matches": [
            {"path": disp, "line": 5, "text": "class Dispatcher:"},
            {"path": disp, "line": 9, "text": "dispatcher = X"},
            {"path": ag, "line": 410, "text": "dispatcher.status"},
        ],
    }
    answer = _format_filesystem_find_answer("dispatcher", data)
    assert "Найдено 3 совпадений" in answer
    assert "2 файлах" in answer
    # names the REAL files (ranked by hit count), not just a bare count
    assert "dispatcher.py" in answer
    assert "agent.py" in answer
    assert answer.index("dispatcher.py") < answer.index("agent.py")  # 2 hits ranked first
    assert "усечён" in answer  # truncation surfaced
    # no matches → honest "nothing found"
    empty = _format_filesystem_find_answer("zzz", {"root": "D:\\x", "matches": []})
    assert "Ничего не найдено" in empty


def test_filesystem_find_answer_only_for_that_intent():
    from jarvis_gpt.agent import (
        TaskKernelPlan,
        _ExecutedToolResult,
        _filesystem_find_answer,
    )
    from jarvis_gpt.models import ToolRunResponse

    find_tool = _ExecutedToolResult(
        tool="filesystem.find",
        arguments={"query": "dispatcher", "path": r"D:\jarvis-gpt\backend"},
        result=ToolRunResponse(
            tool="filesystem.find",
            ok=True,
            summary="Found 1 match(es) across 1 file(s).",
            data={"root": r"D:\x", "matches": [{"path": r"D:\x\a.py", "line": 1, "text": "d"}]},
        ),
    )
    find_plan = TaskKernelPlan(
        route="local_action", mode="concise", intent="filesystem.find", confidence=0.82,
    )
    answer = _filesystem_find_answer((find_tool,), find_plan)
    assert answer is not None and "a.py" in answer

    # A different intent must NOT be overridden even if a find happened to run.
    other_plan = TaskKernelPlan(
        route="reasoning", mode="concise", intent="document_memory", confidence=0.9,
    )
    assert _filesystem_find_answer((find_tool,), other_plan) is None
    # No successful find in the executed tools → None.
    assert _filesystem_find_answer((), find_plan) is None
