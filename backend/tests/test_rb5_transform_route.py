"""RB-5: fully specified TRANSFORM_EXISTING_DOCUMENT routes deterministically."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from jarvis_gpt.agent import (
    EXISTING_DOCUMENT_REFERENCE,
    NEW_ARTIFACT_REQUEST,
    TOOL_PROTOCOL_FAILURE_ANSWER,
    TRANSFORM_EXISTING_DOCUMENT,
    AgentRuntime,
    _classify_document_artifact_intent,
    _contains_internal_tool_output,
    _is_fully_specified_transform,
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


class _CountingArbiterLLM(_FailLLM):
    """Counts complete() invocations if any code path reaches the LLM."""


CLAUDE_PROMPT = (
    "на основе загруженного документа src-doc.txt подготовь markdown-файл "
    "с именем {name} в каталоге document-outputs"
)

EN_PROMPT = (
    "Convert the uploaded source-doc.txt to markdown and save as "
    "{name} in document-outputs"
)


def _seed_source(
    storage: JarvisStorage,
    settings,
    *,
    name: str = "src-doc.txt",
    body: str = "SOURCE_MARKER_RB5\nsecond line\n",
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


def test_rb5_a_fully_specified_transform_100_deterministic_routes(
    monkeypatch, tmp_path: Path
) -> None:
    """A: 100/100 deterministic route selections for fully specified transform."""
    routes: list[str] = []
    for i in range(100):
        prompt = CLAUDE_PROMPT.format(name=f"tr-rb5-a-{i}.md")
        assert _is_fully_specified_transform(prompt)
        assert _classify_document_artifact_intent(prompt) == TRANSFORM_EXISTING_DOCUMENT
        intent = _new_artifact_intent_from_message(prompt)
        assert intent is not None
        assert intent["complete"] is True
        assert intent["kind"] == TRANSFORM_EXISTING_DOCUMENT
        assert intent["filename"] == f"tr-rb5-a-{i}.md"
        assert intent["source_filename"] == "src-doc.txt"
        routes.append(intent["kind"])
    assert routes == [TRANSFORM_EXISTING_DOCUMENT] * 100


def test_rb5_b_no_generic_arbiter_for_complete_transform(
    monkeypatch, tmp_path: Path
) -> None:
    """B: generic intent arbiter is never consulted for complete transform."""
    agent, storage, settings, llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    arbiter_calls = {"n": 0}
    orig = agent._understand_intent

    async def wrap(message, context):
        arbiter_calls["n"] += 1
        return await orig(message, context)

    agent._understand_intent = wrap  # type: ignore[method-assign]
    prompt = CLAUDE_PROMPT.format(name="tr-rb5-b.md")
    response = asyncio.run(agent.chat(prompt))
    expected = Path(settings.data_dir) / "document-outputs" / "tr-rb5-b.md"
    assert expected.is_file()
    assert "Артефакт создан" in response.answer
    assert arbiter_calls["n"] == 0
    assert llm.calls == 0
    storage.close()


def test_rb5_c_no_mission_plan_for_single_step_transform(
    monkeypatch, tmp_path: Path
) -> None:
    """C: single-step transform never creates a mission plan."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    prompt = CLAUDE_PROMPT.format(name="tr-rb5-c.md")
    response = asyncio.run(agent.chat(prompt))
    assert response.mission_id is None
    assert "mission plan" not in response.answer.casefold()
    assert "миссию" not in response.answer.casefold()
    assert (Path(settings.data_dir) / "document-outputs" / "tr-rb5-c.md").is_file()
    storage.close()


def test_rb5_d_typed_trusted_invocation_not_blocked_by_payload_guard(
    monkeypatch, tmp_path: Path
) -> None:
    """D: typed trusted convert path is not blocked as internal payload leak."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    prompt = CLAUDE_PROMPT.format(name="tr-rb5-d.md")
    response = asyncio.run(agent.chat(prompt))
    assert TOOL_PROTOCOL_FAILURE_ANSWER not in response.answer
    assert "внутренний вызов инструмента" not in response.answer
    assert "Артефакт создан" in response.answer
    assert any(
        e.type == "tool_call" and e.title == "documents.convert" for e in response.events
    )
    storage.close()


def test_rb5_e_model_generated_tool_envelope_still_blocked() -> None:
    """E: model-generated tool envelope is blocked and not shown to the user."""
    raw = '{"tool":"documents.convert","arguments":{"path":"/x","output_format":"md"}}'
    assert _contains_internal_tool_output(raw) is True
    assert _user_visible_answer(raw) == TOOL_PROTOCOL_FAILURE_ANSWER
    fenced = "```json\n" + raw + "\n```"
    assert _user_visible_answer(fenced) == TOOL_PROTOCOL_FAILURE_ANSWER
    call_marker = "call:documents.convert path=/tmp/x"
    assert _user_visible_answer(call_marker) == TOOL_PROTOCOL_FAILURE_ANSWER


def test_rb5_f_incomplete_transform_clarifies_without_side_effects(
    monkeypatch, tmp_path: Path
) -> None:
    """F: incomplete transform asks one clarification; no artifact/mission/tool."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    before = list((Path(settings.data_dir) / "document-outputs").glob("*")) if (
        Path(settings.data_dir) / "document-outputs"
    ).exists() else []
    prompt = "на основе загруженного документа src-doc.txt подготовь файл в нужном формате"
    response = asyncio.run(agent.chat(prompt))
    after_root = Path(settings.data_dir) / "document-outputs"
    after = list(after_root.glob("*")) if after_root.exists() else []
    assert response.mission_id is None
    assert "Артефакт создан" not in response.answer
    assert "?" in response.answer or "Уточните" in response.answer
    assert len(after) == len(before)
    storage.close()


def test_rb5_g_multi_step_composite_may_mission_only_with_extra_steps(
    monkeypatch, tmp_path: Path
) -> None:
    """G: multi-step composite may create mission only with real extra steps."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    # Fully specified single-step transform must stay sealed.
    _seed_source(storage, settings, name="src-doc.txt")
    single = CLAUDE_PROMPT.format(name="tr-rb5-g-single.md")
    resp_single = asyncio.run(agent.chat(single))
    assert resp_single.mission_id is None
    # Explicit multi-step mission language without complete transform operands.
    multi = (
        "Создай mission plan: полностью спроектируй и реализуй многошаговый проект "
        "архитектуры пайплайна с нуля, разложи на шаги и задачи"
    )
    ctx = agent._prepare_context(multi, None)
    plan = agent._plan_task(multi, ctx, mode="auto", attachments=[])
    assert plan.route == "mission" or agent._looks_like_mission(multi)
    storage.close()


def test_rb5_h_existing_document_recall_remains_recall(
    monkeypatch, tmp_path: Path
) -> None:
    """H: existing-document recall remains recall."""
    prompt = "Дай резюме сохранённого документа phoenix"
    assert _classify_document_artifact_intent(prompt) == EXISTING_DOCUMENT_REFERENCE
    assert _new_artifact_intent_from_message(prompt) is None
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    ctx = agent._prepare_context(prompt, None)
    plan = agent._plan_task(prompt, ctx, mode="auto", attachments=[])
    assert plan.intent == "document_memory"
    assert "documents.recall" in plan.tools
    storage.close()


def test_rb5_i_direct_new_artifact_remains_generation(
    monkeypatch, tmp_path: Path
) -> None:
    """I: direct NEW_ARTIFACT_REQUEST remains generation."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    prompt = (
        "Create a new markdown file named rb5-new-i.md in document-outputs "
        "with content: hello RB5 I"
    )
    assert _classify_document_artifact_intent(prompt) == NEW_ARTIFACT_REQUEST
    response = asyncio.run(agent.chat(prompt))
    expected = Path(settings.data_dir) / "document-outputs" / "rb5-new-i.md"
    assert expected.is_file()
    assert "hello RB5 I" in expected.read_text(encoding="utf-8")
    assert "Артефакт создан" in response.answer
    storage.close()


def test_rb5_j_exact_destination_verification_rb4_still_required(
    monkeypatch, tmp_path: Path
) -> None:
    """J: exact destination verification from RB-4 remains mandatory."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    wrong = Path(settings.data_dir) / "document-outputs" / "wrong-rb5-j.md"
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
    response = asyncio.run(
        agent.chat(CLAUDE_PROMPT.format(name="tr-rb5-j-claim.md"))
    )
    claimed = Path(settings.data_dir) / "document-outputs" / "tr-rb5-j-claim.md"
    assert not claimed.exists()
    assert "Артефакт создан" not in response.answer
    storage.close()


def test_rb5_k_tool_failure_no_fallback_to_mission_or_search(
    monkeypatch, tmp_path: Path
) -> None:
    """K: tool failure does not fall back to mission/search."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")

    async def fake_fail(name, arguments=None, **kwargs):
        return ToolRunResponse(
            tool="documents.convert",
            ok=False,
            summary="convert failed closed",
            data={},
        )

    monkeypatch.setattr(agent.tools, "run", fake_fail)
    response = asyncio.run(agent.chat(CLAUDE_PROMPT.format(name="tr-rb5-k.md")))
    assert response.mission_id is None
    assert "mission plan" not in response.answer.casefold()
    assert "Артефакт создан" not in response.answer
    assert not (Path(settings.data_dir) / "document-outputs" / "tr-rb5-k.md").exists()
    storage.close()


def test_rb5_l_two_conversations_do_not_mix_transform_contracts(
    monkeypatch, tmp_path: Path
) -> None:
    """L: two conversations do not mix transform contracts."""
    agent, storage, settings, _llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    a = asyncio.run(
        agent.chat(
            CLAUDE_PROMPT.format(name="tr-rb5-l-a.md"),
            conversation_id=None,
        )
    )
    conv_a = a.conversation_id
    b = asyncio.run(
        agent.chat(
            CLAUDE_PROMPT.format(name="tr-rb5-l-b.md"),
            conversation_id=None,
        )
    )
    conv_b = b.conversation_id
    assert conv_a != conv_b
    path_a = Path(settings.data_dir) / "document-outputs" / "tr-rb5-l-a.md"
    path_b = Path(settings.data_dir) / "document-outputs" / "tr-rb5-l-b.md"
    assert path_a.is_file() and path_b.is_file()
    assert "tr-rb5-l-a.md" in a.answer
    assert "tr-rb5-l-b.md" in b.answer
    assert "tr-rb5-l-b.md" not in a.answer
    assert "tr-rb5-l-a.md" not in b.answer
    storage.close()


def test_rb5_english_and_claude_prompts_share_deterministic_path(
    monkeypatch, tmp_path: Path
) -> None:
    """English convert and Claude Russian prepare both hit convert fast path."""
    agent, storage, settings, llm = _agent(monkeypatch, tmp_path)
    _seed_source(storage, settings, name="src-doc.txt")
    _seed_source(storage, settings, name="source-doc.txt")
    for prompt, name in (
        (CLAUDE_PROMPT.format(name="tr-rb5-ru.md"), "tr-rb5-ru.md"),
        (EN_PROMPT.format(name="tr-rb5-en.md"), "tr-rb5-en.md"),
    ):
        assert _classify_document_artifact_intent(prompt) == TRANSFORM_EXISTING_DOCUMENT
        resp = asyncio.run(agent.chat(prompt))
        assert (Path(settings.data_dir) / "document-outputs" / name).is_file()
        assert "Артефакт создан" in resp.answer
        assert resp.mission_id is None
    assert llm.calls == 0
    storage.close()
