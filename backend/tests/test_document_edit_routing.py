"""NL edit-intent routing: "edit the existing document" requests must resolve the
target file and route to documents.edit, under owner autonomy, without asking for a
file name."""

from __future__ import annotations

import asyncio

from jarvis_gpt.agent import (
    AgentContext,
    AgentRuntime,
    _looks_like_document_edit_query,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.document_runtime import write_markdown_docx, write_workbook_xlsx
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage


def _agent(monkeypatch, tmp_path, *, autonomy: bool = True):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "1" if autonomy else "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus()
    )
    return agent, storage, settings


def _ingest_xlsx(settings, storage, tmp_path, name="бюджет.xlsx"):
    path = tmp_path / name
    write_workbook_xlsx(
        path,
        [{"name": "Budget", "rows": [["Статья", "Сумма"], ["Аренда", 30000]]}],
        title="Budget",
    )
    return FileIngestor(settings=settings, storage=storage).ingest_path(path)


def _route(agent, message):
    ctx = AgentContext(conversation_id="c", memory_hits=[], file_hits=[])
    return agent._plan_task(message, ctx, mode="auto", attachments=[])


# --------------------------------------------------------------- detection matrix


def test_edit_detection_positive():
    assert _looks_like_document_edit_query(
        "исправь дату в отчёте на 20 июля", has_persisted_files=True
    )
    assert _looks_like_document_edit_query(
        "добавь строку в бюджет.xlsx: Реклама 15000", has_persisted_files=True
    )
    assert _looks_like_document_edit_query("измени report.docx", has_persisted_files=False)
    assert _looks_like_document_edit_query(
        "допиши раздел про риски в этот документ", has_persisted_files=True
    )
    assert _looks_like_document_edit_query(
        "замени в договоре сумму на 500000", has_persisted_files=True
    )


def test_edit_detection_negative():
    # create-new (no edit verb) stays out
    assert not _looks_like_document_edit_query(
        "создай новый отчёт по проекту", has_persisted_files=True
    )
    # recall/summarize (no edit verb) stays document_memory
    assert not _looks_like_document_edit_query(
        "сделай выжимку из документа про архитектуру", has_persisted_files=True
    )
    # live-web must never be captured
    assert not _looks_like_document_edit_query(
        "добавь в корзину и купи rtx 5090 подешевле", has_persisted_files=True
    )
    # "добавь" without a document anchor is not an edit
    assert not _looks_like_document_edit_query(
        "добавь молоко в список покупок", has_persisted_files=True
    )
    assert not _looks_like_document_edit_query("добавь пункт", has_persisted_files=True)


# ------------------------------------------------------------------- target resolve


def test_resolve_edit_target_by_name(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    ingested = _ingest_xlsx(settings, storage, tmp_path, "бюджет.xlsx")
    record = agent._resolve_document_edit_target("добавь строку в бюджет.xlsx")
    assert record is not None
    assert record["id"] == ingested["file"]["id"]
    storage.close()


def test_resolve_edit_target_most_recent_editable(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    path = tmp_path / "notes.docx"
    write_markdown_docx(path, "# Notes\n\nbody\n", title="Notes")
    FileIngestor(settings=settings, storage=storage).ingest_path(path)
    record = agent._resolve_document_edit_target("допиши в этот документ раздел про риски")
    assert record is not None and record["name"] == "notes.docx"
    storage.close()


def test_resolve_edit_target_none_when_empty(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    assert agent._resolve_document_edit_target("измени бюджет.xlsx") is None
    storage.close()


# ------------------------------------------------------------------------- routing


def test_edit_request_routes_to_document_edit(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path, autonomy=True)
    _ingest_xlsx(settings, storage, tmp_path, "бюджет.xlsx")
    plan = _route(agent, "добавь строку в бюджет.xlsx: Реклама 15000")
    assert plan.intent == "document_edit"
    assert "documents.edit" in plan.tools
    storage.close()


def test_edit_request_without_file_falls_through(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path, autonomy=True)
    plan = _route(agent, "добавь строку в бюджет.xlsx: Реклама 15000")
    assert plan.intent != "document_edit"
    storage.close()


def test_edit_routing_is_autonomy_gated(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path, autonomy=False)
    _ingest_xlsx(settings, storage, tmp_path, "бюджет.xlsx")
    plan = _route(agent, "добавь строку в бюджет.xlsx: Реклама 15000")
    assert plan.intent != "document_edit"
    storage.close()


# ------------------------------------------------------------------------ prefetch


def test_prefetch_document_edit_builds_observation(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path, autonomy=True)
    ingested = _ingest_xlsx(settings, storage, tmp_path, "бюджет.xlsx")
    ctx = AgentContext(conversation_id="c", memory_hits=[], file_hits=[])
    ctx.task_plan = _route(agent, "добавь строку в бюджет.xlsx: Реклама 15000")
    prefetch = asyncio.run(
        agent._prefetch_document_edit("добавь строку в бюджет.xlsx: Реклама 15000", ctx)
    )
    assert prefetch is not None
    observation, event, tool_result = prefetch
    fid = ingested["file"]["id"]
    assert "documents.edit" in observation
    assert fid in observation
    assert "бюджет.xlsx" in observation
    assert event.payload["file_ids"] == [fid]
    assert tool_result.ok is True
    storage.close()
