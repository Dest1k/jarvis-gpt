"""RB-4: TRANSFORM_EXISTING_DOCUMENT binds exact destination before tool execution."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from jarvis_gpt.agent import (
    EXISTING_DOCUMENT_REFERENCE,
    NEW_ARTIFACT_REQUEST,
    TRANSFORM_EXISTING_DOCUMENT,
    AgentRuntime,
    _classify_document_artifact_intent,
    _destination_filename_from_message,
    _new_artifact_intent_from_message,
    _source_filename_from_message,
    _verified_artifact_answer,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


class _FailLLM:
    async def complete(self, messages, *, temperature=None, max_tokens=None):
        raise AssertionError("LLM must not run for deterministic document routes")


def _seed_source(storage: JarvisStorage, settings, *, name: str = "source-doc.txt") -> dict:
    body = "SOURCE_MARKER_RB4\nsecond line\n"
    path = Path(settings.data_dir) / "files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rec = storage.create_file_record(
        name=name,
        source_path=None,
        stored_path=path,
        sha256=digest,
        size=len(body.encode()),
        mime_type="text/plain",
        status="ready",
        error=None,
        chunk_count=1,
    )
    storage.add_file_chunks(rec["id"], [body])
    return {
        "id": rec["id"],
        "path": path,
        "sha256": digest,
        "body": body,
        "name": name,
    }


def test_rb4_a_transform_exact_path(monkeypatch, tmp_path: Path) -> None:
    """A: source + requested output → exact requested file is created."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = _seed_source(storage, settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())
    prompt = (
        "Convert the uploaded source-doc.txt to markdown and save as "
        "tr-exact-a.md in document-outputs"
    )
    assert _classify_document_artifact_intent(prompt) == TRANSFORM_EXISTING_DOCUMENT
    intent = _new_artifact_intent_from_message(prompt)
    assert intent is not None
    assert intent["filename"] == "tr-exact-a.md"
    assert intent["source_filename"] == "source-doc.txt"
    assert "markdown/" not in intent["output_name"]

    response = asyncio.run(agent.chat(prompt))
    expected = Path(settings.data_dir) / "document-outputs" / "tr-exact-a.md"
    assert expected.is_file()
    assert expected.read_text(encoding="utf-8")
    assert "Артефакт создан" in response.answer
    assert "tr-exact-a.md" in response.answer
    assert hashlib.sha256(source["path"].read_bytes()).hexdigest() == source["sha256"]
    storage.close()


def test_rb4_b_mismatched_tool_path_is_failure(monkeypatch, tmp_path: Path) -> None:
    """B: tool returns another existing path → verification FAIL, no success."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    _seed_source(storage, settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())

    wrong = Path(settings.data_dir) / "document-outputs" / "source-doc.txt"
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text("wrong output\n", encoding="utf-8")

    async def fake_convert(name, arguments=None, **kwargs):
        assert name == "documents.convert"
        return ToolRunResponse(
            tool="documents.convert",
            ok=True,
            summary="Converted to wrong path",
            data={
                "ok": True,
                "actual_path": str(wrong),
                "requested_destination": str(
                    Path(settings.data_dir) / "document-outputs" / "tr-claim-b.md"
                ),
                "output": {
                    "path": str(wrong),
                    "name": wrong.name,
                    "format": "md",
                    "size": wrong.stat().st_size,
                    "sha256": "deadbeef",
                },
                "path_verification": {
                    "ok": True,
                    "path": str(wrong),
                    "name": wrong.name,
                },
                "source_hash_before": "aaa",
                "source_hash_after": "aaa",
                "output_hash": "deadbeef",
                "output_size": wrong.stat().st_size,
                "validation_result": {"ok": True, "path": str(wrong)},
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_convert)
    response = asyncio.run(
        agent.chat(
            "Convert the uploaded source-doc.txt to markdown named tr-claim-b.md "
            "in document-outputs"
        )
    )
    claimed = Path(settings.data_dir) / "document-outputs" / "tr-claim-b.md"
    assert not claimed.exists()
    assert "Артефакт создан" not in response.answer
    storage.close()


def test_rb4_c_source_path_as_output_is_failure(monkeypatch, tmp_path: Path) -> None:
    """C: tool returns source path as output → FAIL."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = _seed_source(storage, settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())

    async def fake_convert(name, arguments=None, **kwargs):
        return ToolRunResponse(
            tool="documents.convert",
            ok=True,
            summary="Returned source as output",
            data={
                "ok": True,
                "actual_path": str(source["path"]),
                "output": {
                    "path": str(source["path"]),
                    "name": source["path"].name,
                    "format": "txt",
                    "size": source["path"].stat().st_size,
                    "sha256": source["sha256"],
                },
                "path_verification": {
                    "ok": True,
                    "path": str(source["path"]),
                    "name": source["path"].name,
                },
                "validation_result": {"ok": True, "path": str(source["path"])},
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_convert)
    response = asyncio.run(
        agent.chat(
            "Convert the uploaded source-doc.txt to markdown named tr-claim-c.md "
            "in document-outputs"
        )
    )
    assert "Артефакт создан" not in response.answer
    assert hashlib.sha256(source["path"].read_bytes()).hexdigest() == source["sha256"]
    storage.close()


def test_rb4_d_directory_destination_no_side_effects(monkeypatch, tmp_path: Path) -> None:
    """D: destination is a directory → clarification/error, no side effects."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    _seed_source(storage, settings)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    result = asyncio.run(
        tools.run(
            "documents.convert",
            {
                "path": str(Path(settings.data_dir) / "files" / "source-doc.txt"),
                "output_format": "md",
                "output_name": "document-outputs",
                "require_exact_path": True,
            },
        )
    )
    assert result.ok is False
    out_root = Path(settings.data_dir) / "document-outputs"
    # No regular file named document-outputs under outputs.
    if out_root.exists():
        assert not (out_root / "document-outputs").is_file()
    storage.close()


def test_rb4_e_format_label_subdirectory_fails(monkeypatch, tmp_path: Path) -> None:
    """E: format label must not become a destination subdirectory."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    prompt = (
        "Преобразуй загруженный source-doc.txt в markdown и сохрани как tr-fmt-e.md"
    )
    intent = _new_artifact_intent_from_message(prompt)
    assert intent is not None
    assert intent["filename"] == "tr-fmt-e.md"
    assert not intent["output_name"].lower().startswith("markdown")
    assert "/markdown" not in intent["output_name"].lower()
    assert intent["requested_destination"] == "document-outputs/tr-fmt-e.md"


def test_rb4_f_timestamp_fallback_forbids_success(monkeypatch, tmp_path: Path) -> None:
    """F: timestamp fallback is FAIL; requested path is not declared created."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    _seed_source(storage, settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())
    stamped = (
        Path(settings.data_dir)
        / "document-outputs"
        / "tr-ts-f.20260715120000.md"
    )
    stamped.parent.mkdir(parents=True, exist_ok=True)
    stamped.write_text("# stamped\n", encoding="utf-8")

    async def fake_convert(name, arguments=None, **kwargs):
        return ToolRunResponse(
            tool="documents.convert",
            ok=True,
            summary="timestamp fallback",
            data={
                "ok": True,
                "actual_path": str(stamped),
                "output": {
                    "path": str(stamped),
                    "name": stamped.name,
                    "format": "md",
                    "size": stamped.stat().st_size,
                    "sha256": "ts",
                },
                "path_verification": {
                    "ok": True,
                    "path": str(stamped),
                    "name": stamped.name,
                },
                "validation_result": {"ok": True, "path": str(stamped)},
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_convert)
    response = asyncio.run(
        agent.chat(
            "Convert the uploaded source-doc.txt to markdown named tr-ts-f.md "
            "in document-outputs"
        )
    )
    requested = Path(settings.data_dir) / "document-outputs" / "tr-ts-f.md"
    assert not requested.exists()
    assert "Артефакт создан" not in response.answer
    storage.close()


def test_rb4_g_collision_without_overwrite(monkeypatch, tmp_path: Path) -> None:
    """G: existing destination without overwrite → FAIL, hash unchanged."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = _seed_source(storage, settings)
    occupied = Path(settings.data_dir) / "document-outputs" / "tr-collide-g.md"
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_text("OCCUPIED_MARKER\n", encoding="utf-8")
    before = hashlib.sha256(occupied.read_bytes()).hexdigest()
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())
    response = asyncio.run(
        agent.chat(
            "Convert the uploaded source-doc.txt to markdown named tr-collide-g.md "
            "in document-outputs"
        )
    )
    assert "Артефакт создан" not in response.answer
    assert occupied.read_text(encoding="utf-8") == "OCCUPIED_MARKER\n"
    assert hashlib.sha256(occupied.read_bytes()).hexdigest() == before
    assert hashlib.sha256(source["path"].read_bytes()).hexdigest() == source["sha256"]
    storage.close()


def test_rb4_h_source_hash_unchanged(monkeypatch, tmp_path: Path) -> None:
    """H: source hash remains unchanged after successful transform."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = _seed_source(storage, settings)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    result = asyncio.run(
        tools.run(
            "documents.convert",
            {
                "file_id": source["id"],
                "output_format": "md",
                "output_name": "tr-hash-h.md",
                "require_exact_path": True,
            },
        )
    )
    assert result.ok is True
    assert result.data["source_unchanged"] is True
    assert result.data["source_hash_before"] == source["sha256"]
    assert result.data["source_hash_after"] == source["sha256"]
    assert hashlib.sha256(source["path"].read_bytes()).hexdigest() == source["sha256"]
    out = Path(result.data["actual_path"])
    assert out.name == "tr-hash-h.md"
    assert out.is_file()
    storage.close()


def test_rb4_i_final_response_path_from_verified_result_only(
    monkeypatch, tmp_path: Path
) -> None:
    """I: final response cannot contain a path absent from verified result."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    _seed_source(storage, settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())

    async def fake_convert(name, arguments=None, **kwargs):
        # Tool claims success but provides no usable verified path.
        return ToolRunResponse(
            tool="documents.convert",
            ok=False,
            summary="convert failed closed",
            data={},
        )

    monkeypatch.setattr(agent.tools, "run", fake_convert)
    response = asyncio.run(
        agent.chat(
            "Convert the uploaded source-doc.txt to markdown named tr-noverify-i.md "
            "in document-outputs"
        )
    )
    assert "Артефакт создан" not in response.answer
    # Must not invent a successful path claim for the requested name as created.
    lower = response.answer.casefold()
    assert "создан" not in lower or "не" in lower or "ошиб" in lower
    storage.close()


def test_rb4_j_existing_document_reference_stays_recall(monkeypatch, tmp_path: Path) -> None:
    """J: EXISTING_DOCUMENT_REFERENCE remains recall/search."""
    prompt = "Дай резюме сохранённого документа phoenix"
    assert _classify_document_artifact_intent(prompt) == EXISTING_DOCUMENT_REFERENCE
    assert _new_artifact_intent_from_message(prompt) is None

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.create_file_record(
        name="phoenix-plan.md",
        source_path=None,
        stored_path=tmp_path / "phoenix-plan.md",
        sha256="abc",
        size=12,
        mime_type="text/markdown",
        status="ready",
        error=None,
        chunk_count=1,
    )
    (tmp_path / "phoenix-plan.md").write_text("phoenix body\n", encoding="utf-8")
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())
    ctx = agent._prepare_context(prompt, None)
    plan = agent._plan_task(prompt, ctx, mode="auto", attachments=[])
    assert plan.intent == "document_memory"
    assert "documents.recall" in plan.tools
    storage.close()


def test_rb4_k_new_artifact_request_stays_direct_generation(
    monkeypatch, tmp_path: Path
) -> None:
    """K: NEW_ARTIFACT_REQUEST remains direct generation at exact path."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())
    prompt = (
        "Create a new markdown file named rb4-new-k.md in document-outputs "
        "with content: hello RB4 K"
    )
    assert _classify_document_artifact_intent(prompt) == NEW_ARTIFACT_REQUEST
    response = asyncio.run(agent.chat(prompt))
    expected = Path(settings.data_dir) / "document-outputs" / "rb4-new-k.md"
    assert expected.is_file()
    assert "hello RB4 K" in expected.read_text(encoding="utf-8")
    assert "Артефакт создан" in response.answer
    assert "rb4-new-k.md" in response.answer
    storage.close()


def test_rb4_l_conversations_do_not_mix_source_destination_contracts(
    monkeypatch, tmp_path: Path
) -> None:
    """L: two conversations keep independent source/destination contracts."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = _seed_source(storage, settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=_FailLLM(), bus=EventBus())

    a = asyncio.run(
        agent.chat(
            "Convert the uploaded source-doc.txt to markdown named conv-a.md "
            "in document-outputs"
        )
    )
    b = asyncio.run(
        agent.chat(
            "Convert the uploaded source-doc.txt to markdown named conv-b.md "
            "in document-outputs"
        )
    )
    assert a.conversation_id != b.conversation_id
    out_a = Path(settings.data_dir) / "document-outputs" / "conv-a.md"
    out_b = Path(settings.data_dir) / "document-outputs" / "conv-b.md"
    assert out_a.is_file()
    assert out_b.is_file()
    assert out_a.resolve() != out_b.resolve()
    assert "conv-a.md" in a.answer
    assert "conv-b.md" in b.answer
    assert "conv-b.md" not in a.answer
    assert "conv-a.md" not in b.answer
    assert hashlib.sha256(source["path"].read_bytes()).hexdigest() == source["sha256"]
    storage.close()


def test_rb4_source_and_destination_fields_never_swap() -> None:
    prompt = (
        "Преобразуй загруженный source-doc.txt в markdown и сохрани как dest-only.md"
    )
    assert _source_filename_from_message(prompt) == "source-doc.txt"
    assert (
        _destination_filename_from_message(
            prompt, source_filename="source-doc.txt"
        )
        == "dest-only.md"
    )
    intent = _new_artifact_intent_from_message(prompt)
    assert intent is not None
    assert intent["source_filename"] == "source-doc.txt"
    assert intent["filename"] == "dest-only.md"
    assert intent["source_filename"] != intent["filename"]


def test_rb4_verified_answer_rejects_path_outside_allowed_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("x\n", encoding="utf-8")
    allowed = tmp_path / "document-outputs"
    allowed.mkdir(parents=True, exist_ok=True)
    result = ToolRunResponse(
        tool="documents.convert",
        ok=True,
        summary="ok",
        data={
            "actual_path": str(outside),
            "output": {"path": str(outside), "name": "outside.md"},
            "path_verification": {"ok": True, "path": str(outside)},
            "validation_result": {"ok": True, "path": str(outside)},
        },
    )
    ok, path, answer = _verified_artifact_answer(
        result=result,
        intent={
            "filename": "outside.md",
            "output_name": "outside.md",
            "requested_destination": "document-outputs/outside.md",
            "output_format": "md",
        },
        allowed_root=allowed,
    )
    assert ok is False
    assert "Артефакт создан" not in answer
    assert path == str(outside)
