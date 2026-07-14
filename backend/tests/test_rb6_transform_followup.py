"""RB-6: clarification follow-up restores typed TRANSFORM pending contract."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from jarvis_gpt.agent import (
    EXISTING_DOCUMENT_REFERENCE,
    TOOL_PROTOCOL_FAILURE_ANSWER,
    TRANSFORM_EXISTING_DOCUMENT,
    AgentRuntime,
    _build_pending_transform_draft,
    _classify_document_artifact_intent,
    _contains_internal_tool_output,
    _intent_from_transform_draft,
    _is_fully_specified_transform,
    _merge_transform_draft_followup,
    _new_artifact_intent_from_message,
    _user_visible_answer,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


class _FailLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature=None, max_tokens=None):
        self.calls += 1
        raise AssertionError("LLM must not run for deterministic document routes")


INCOMPLETE = (
    "На основе загруженного документа {src} подготовь отчёт в нужном формате "
    "и положи куда следует."
)
FOLLOWUP = (
    "Формат markdown, имя файла {dest}, каталог document-outputs."
)
COMPLETE = (
    "на основе загруженного документа {src} подготовь markdown-файл "
    "с именем {dest} в каталоге document-outputs"
)


def _seed_source(
    storage: JarvisStorage,
    settings,
    *,
    name: str = "src-doc.txt",
    body: str = "SOURCE_MARKER_RB6\nsecond line\n",
) -> dict:
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


def _agent(monkeypatch, tmp_path: Path, *, llm=None):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    runtime_llm = llm or _FailLLM()
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=runtime_llm, bus=EventBus()
    )
    return agent, storage, settings, runtime_llm


def _pending(storage: JarvisStorage, conversation_id: str) -> dict | None:
    value = storage.get_runtime_value(f"clarification.pending.{conversation_id}", None)
    return value if isinstance(value, dict) else None


def test_rb6_a_incomplete_transform_stores_typed_pending_draft(
    monkeypatch, tmp_path: Path
) -> None:
    """A: incomplete transform stores typed pending draft."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-a.txt")
    prompt = INCOMPLETE.format(src="src-rb6-a.txt")
    response = asyncio.run(agent.chat(prompt))
    assert "?" in response.answer or "Уточните" in response.answer
    pending = _pending(storage, response.conversation_id)
    assert pending is not None
    assert pending.get("goal")
    draft = pending.get("draft")
    assert isinstance(draft, dict)
    assert draft.get("intent_kind") == TRANSFORM_EXISTING_DOCUMENT
    assert draft.get("source_filename") == "src-rb6-a.txt"
    assert "format" in (draft.get("missing_fields") or [])
    assert "destination" in (draft.get("missing_fields") or [])
    assert draft.get("destination_filename") in (None, "")
    assert draft.get("conversation_id") == response.conversation_id
    assert not (Path(settings.data_dir) / "document-outputs").exists() or not list(
        (Path(settings.data_dir) / "document-outputs").glob("*")
    )
    storage.close()


def test_rb6_b_followup_restores_transform_existing_document(
    monkeypatch, tmp_path: Path
) -> None:
    """B: follow-up format + filename restores TRANSFORM_EXISTING_DOCUMENT."""
    agent, storage, settings, llm = _agent(monkeypatch, tmp_path)
    source = _seed_source(storage, settings, name="src-rb6-b.txt")
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-b.txt")))
    second = asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-b.md"),
            conversation_id=first.conversation_id,
        )
    )
    expected = Path(settings.data_dir) / "document-outputs" / "want-rb6-b.md"
    assert expected.is_file()
    assert "want-rb6-b.md" in second.answer
    # Final claim must not present the source basename as the created artifact.
    assert "Файл: `src-rb6-b.txt`" not in second.answer
    assert "Артефакт создан" in second.answer or "подготовлен" in second.answer.casefold()
    assert any(
        e.type == "tool_call" and e.title == "documents.convert" for e in second.events
    )
    assert llm.calls == 0
    assert hashlib.sha256(source["path"].read_bytes()).hexdigest() == source["sha256"]
    storage.close()


def test_rb6_c_source_filename_never_becomes_destination(
    monkeypatch, tmp_path: Path
) -> None:
    """C: source filename is never used as destination."""
    draft = _build_pending_transform_draft(
        INCOMPLETE.format(src="src-rb6-c.txt"),
        conversation_id="conv-c",
        gaps=["format", "destination"],
    )
    assert draft is not None
    merged = _merge_transform_draft_followup(
        draft, FOLLOWUP.format(dest="want-rb6-c.md")
    )
    assert merged.get("destination_filename") == "want-rb6-c.md"
    assert merged.get("destination_filename") != "src-rb6-c.txt"
    intent = _intent_from_transform_draft(merged)
    assert intent is not None
    assert intent["filename"] == "want-rb6-c.md"
    assert intent["source_filename"] == "src-rb6-c.txt"

    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-c.txt")
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-c.txt")))
    second = asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-c.md"),
            conversation_id=first.conversation_id,
        )
    )
    out_root = Path(settings.data_dir) / "document-outputs"
    assert (out_root / "want-rb6-c.md").is_file()
    assert not (out_root / "src-rb6-c.txt").exists()
    assert "src-rb6-c.txt" not in second.answer or "want-rb6-c.md" in second.answer
    assert "want-rb6-c.md" in second.answer
    storage.close()


def test_rb6_d_followup_uses_deterministic_executor(
    monkeypatch, tmp_path: Path
) -> None:
    """D: follow-up uses deterministic executor — zero arbiter/mission/free loop."""
    agent, storage, settings, llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-d.txt")
    arbiter_calls = {"n": 0}
    orig = agent._understand_intent

    async def wrap(message, context):
        arbiter_calls["n"] += 1
        return await orig(message, context)

    agent._understand_intent = wrap  # type: ignore[method-assign]
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-d.txt")))
    second = asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-d.md"),
            conversation_id=first.conversation_id,
        )
    )
    assert second.mission_id is None
    assert arbiter_calls["n"] == 0
    assert llm.calls == 0
    assert any(
        e.type == "tool_call" and e.title == "documents.convert" for e in second.events
    )
    assert not any(
        e.type == "tool_call" and e.title == "documents.generate" for e in second.events
    )
    storage.close()


def test_rb6_e_exact_requested_destination_created_and_verified(
    monkeypatch, tmp_path: Path
) -> None:
    """E: exact requested destination is created and verified."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    source = _seed_source(storage, settings, name="src-rb6-e.txt")
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-e.txt")))
    dest = "want-rb6-e-exact.md"
    second = asyncio.run(
        agent.chat(FOLLOWUP.format(dest=dest), conversation_id=first.conversation_id)
    )
    expected = Path(settings.data_dir) / "document-outputs" / dest
    assert expected.is_file()
    assert dest in second.answer
    assert "Артефакт создан" in second.answer or expected.name in second.answer
    assert hashlib.sha256(source["path"].read_bytes()).hexdigest() == source["sha256"]
    tool_events = [e for e in second.events if e.type == "tool_call"]
    assert tool_events
    assert tool_events[-1].payload.get("path_verified") is True
    assert Path(str(tool_events[-1].payload.get("path") or "")).name == dest
    storage.close()


def test_rb6_f_followup_without_filename_asks_again_zero_side_effects(
    monkeypatch, tmp_path: Path
) -> None:
    """F: follow-up without filename asks next clarification; zero side effects."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-f.txt")
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-f.txt")))
    before = list((Path(settings.data_dir) / "document-outputs").glob("*")) if (
        Path(settings.data_dir) / "document-outputs"
    ).exists() else []
    second = asyncio.run(
        agent.chat(
            "Формат markdown, каталог document-outputs.",
            conversation_id=first.conversation_id,
        )
    )
    after_root = Path(settings.data_dir) / "document-outputs"
    after = list(after_root.glob("*")) if after_root.exists() else []
    assert "?" in second.answer or "Уточните" in second.answer
    assert "Артефакт создан" not in second.answer
    assert len(after) == len(before)
    pending = _pending(storage, first.conversation_id)
    assert pending and pending.get("draft")
    assert "destination" in (pending["draft"].get("missing_fields") or [])
    storage.close()


def test_rb6_g_two_conversations_do_not_mix_pending_drafts(
    monkeypatch, tmp_path: Path
) -> None:
    """G: two conversations do not mix pending drafts."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-g1.txt")
    _seed_source(storage, settings, name="src-rb6-g2.txt")
    a = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-g1.txt")))
    b = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-g2.txt")))
    assert a.conversation_id != b.conversation_id
    pa = _pending(storage, a.conversation_id)
    pb = _pending(storage, b.conversation_id)
    assert pa and pb
    assert pa["draft"]["source_filename"] == "src-rb6-g1.txt"
    assert pb["draft"]["source_filename"] == "src-rb6-g2.txt"
    assert pa["draft"]["conversation_id"] == a.conversation_id
    assert pb["draft"]["conversation_id"] == b.conversation_id

    a2 = asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-g1.md"),
            conversation_id=a.conversation_id,
        )
    )
    b2 = asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-g2.md"),
            conversation_id=b.conversation_id,
        )
    )
    out = Path(settings.data_dir) / "document-outputs"
    assert (out / "want-rb6-g1.md").is_file()
    assert (out / "want-rb6-g2.md").is_file()
    assert "want-rb6-g1.md" in a2.answer
    assert "want-rb6-g2.md" in b2.answer
    assert "want-rb6-g2.md" not in a2.answer
    assert "want-rb6-g1.md" not in b2.answer
    storage.close()


def test_rb6_h_retry_reload_no_duplicate_artifact(
    monkeypatch, tmp_path: Path
) -> None:
    """H: retry/reload does not create a duplicate artifact."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-h.txt")
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-h.txt")))
    dest = "want-rb6-h.md"
    asyncio.run(
        agent.chat(FOLLOWUP.format(dest=dest), conversation_id=first.conversation_id)
    )
    expected = Path(settings.data_dir) / "document-outputs" / dest
    assert expected.is_file()
    digest1 = hashlib.sha256(expected.read_bytes()).hexdigest()
    # Repeat the same follow-up after completed state.
    third = asyncio.run(
        agent.chat(FOLLOWUP.format(dest=dest), conversation_id=first.conversation_id)
    )
    matches = list((Path(settings.data_dir) / "document-outputs").glob("want-rb6-h*"))
    assert len(matches) == 1
    assert hashlib.sha256(expected.read_bytes()).hexdigest() == digest1
    assert "Артефакт создан" in third.answer or "повтор" in third.answer.casefold()
    # Must not claim a second convert created a new file under a different name.
    assert not (Path(settings.data_dir) / "document-outputs" / "src-rb6-h.txt").exists()
    storage.close()


def test_rb6_i_repeat_followup_after_completed_no_second_transform(
    monkeypatch, tmp_path: Path
) -> None:
    """I: repeat follow-up after completed state does not run transform twice."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-i.txt")
    convert_calls = {"n": 0}
    real_run = agent.tools.run

    async def tracking_run(name, arguments=None, **kwargs):
        if name == "documents.convert":
            convert_calls["n"] += 1
        return await real_run(name, arguments, **kwargs)

    monkeypatch.setattr(agent.tools, "run", tracking_run)
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-i.txt")))
    asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-i.md"),
            conversation_id=first.conversation_id,
        )
    )
    assert convert_calls["n"] == 1
    asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-i.md"),
            conversation_id=first.conversation_id,
        )
    )
    assert convert_calls["n"] == 1
    storage.close()


def test_rb6_j_direct_fully_specified_transform_remains_deterministic(
    monkeypatch, tmp_path: Path
) -> None:
    """J: direct fully specified transform (RB-5) remains deterministic."""
    agent, storage, settings, llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    arbiter_calls = {"n": 0}
    orig = agent._understand_intent

    async def wrap(message, context):
        arbiter_calls["n"] += 1
        return await orig(message, context)

    agent._understand_intent = wrap  # type: ignore[method-assign]
    prompt = COMPLETE.format(src="src-doc.txt", dest="tr-rb6-j.md")
    assert _is_fully_specified_transform(prompt)
    response = asyncio.run(agent.chat(prompt))
    expected = Path(settings.data_dir) / "document-outputs" / "tr-rb6-j.md"
    assert expected.is_file()
    assert response.mission_id is None
    assert arbiter_calls["n"] == 0
    assert llm.calls == 0
    assert "Артефакт создан" in response.answer
    storage.close()


def test_rb6_k_existing_recall_remains_recall(monkeypatch, tmp_path: Path) -> None:
    """K: existing recall remains recall."""
    prompt = "Дай резюме сохранённого документа phoenix"
    assert _classify_document_artifact_intent(prompt) == EXISTING_DOCUMENT_REFERENCE
    assert _new_artifact_intent_from_message(prompt) is None
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    ctx = agent._prepare_context(prompt, None)
    plan = agent._plan_task(prompt, ctx, mode="auto", attachments=[])
    assert plan.intent == "document_memory"
    assert "documents.recall" in plan.tools
    storage.close()


def test_rb6_l_model_generated_internal_envelope_still_blocked() -> None:
    """L: model-generated internal envelope is still blocked."""
    raw = '{"tool":"documents.convert","arguments":{"path":"/x","output_format":"md"}}'
    assert _contains_internal_tool_output(raw) is True
    assert _user_visible_answer(raw) == TOOL_PROTOCOL_FAILURE_ANSWER
    assert _user_visible_answer("call:documents.convert path=/tmp/x") == (
        TOOL_PROTOCOL_FAILURE_ANSWER
    )


def test_rb6_m_tool_mismatch_no_false_success(
    monkeypatch, tmp_path: Path
) -> None:
    """M: tool mismatch does not yield false success."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-rb6-m.txt")
    wrong = Path(settings.data_dir) / "document-outputs" / "wrong-rb6-m.md"
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text("wrong\n", encoding="utf-8")

    async def fake_convert(name, arguments=None, **kwargs):
        assert name == "documents.convert"
        return ToolRunResponse(
            tool="documents.convert",
            ok=True,
            summary="mismatched path",
            data={
                "ok": True,
                "actual_path": str(wrong),
                "output": {
                    "path": str(wrong),
                    "name": wrong.name,
                    "format": "md",
                    "size": wrong.stat().st_size,
                    "sha256": "dead",
                },
                "path_verification": {
                    "ok": True,
                    "path": str(wrong),
                    "name": wrong.name,
                },
                "validation_result": {"ok": True, "path": str(wrong)},
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_convert)
    first = asyncio.run(agent.chat(INCOMPLETE.format(src="src-rb6-m.txt")))
    second = asyncio.run(
        agent.chat(
            FOLLOWUP.format(dest="want-rb6-m.md"),
            conversation_id=first.conversation_id,
        )
    )
    claimed = Path(settings.data_dir) / "document-outputs" / "want-rb6-m.md"
    assert not claimed.exists()
    assert "Артефакт создан" not in second.answer
    storage.close()


def test_rb6_draft_helpers_unit() -> None:
    """Unit: draft build/merge never defaults destination to source."""
    draft = _build_pending_transform_draft(
        "Convert uploaded src-unit.txt to the right format and put it where it belongs",
        conversation_id="u1",
    )
    assert draft is not None
    assert draft["source_filename"] == "src-unit.txt"
    assert draft["destination_filename"] in (None, "")
    merged = _merge_transform_draft_followup(
        draft, "format md, filename out-unit.md, directory document-outputs"
    )
    assert merged["destination_filename"] == "out-unit.md"
    assert merged["format"] == "md"
    assert merged["missing_fields"] == []
    intent = _intent_from_transform_draft(merged)
    assert intent is not None
    assert intent["kind"] == TRANSFORM_EXISTING_DOCUMENT
    assert intent["complete"] is True
    # Source must not classify as NEW_ARTIFACT when original_goal carries source.
    intent2 = _new_artifact_intent_from_message(
        "format md, filename out-unit2.md, directory document-outputs",
        original_goal="на основе загруженного документа src-unit.txt подготовь отчёт",
    )
    assert intent2 is not None
    assert intent2["kind"] == TRANSFORM_EXISTING_DOCUMENT
    assert intent2["filename"] == "out-unit2.md"
    assert intent2["source_filename"] == "src-unit.txt"
