from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    bind_actor,
    current_actor,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.material_access import MaterialAccessDeniedError
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


class _GroundedLLM:
    async def complete(self, messages, **_kwargs):
        text = str(messages[-1].get("content") or "")
        if '"languages"' in text:
            return SimpleNamespace(
                ok=True,
                content=(
                    '{"ru":"секретный проект","en":"secret project",'
                    '"zh":"秘密项目","ko":"비밀 프로젝트","ja":"秘密プロジェクト"}'
                ),
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        evidence = payload.get("evidence") if isinstance(payload, dict) else []
        if isinstance(evidence, list) and evidence:
            citation = str(evidence[0].get("citation") or "")
            return SimpleNamespace(
                ok=True,
                content=f"Обнаружено упоминание [{citation}].",
            )
        return SimpleNamespace(
            ok=True,
            content="Обнаружено упоминание [message:msg-placeholder].",
        )


def _actor(identity: dict[str, object]) -> ActorContext:
    return ActorContext(
        user_id=str(identity["user_id"]),
        preset_key=str(identity["preset_key"]),
        source="test-session",
        identity_id=str(identity["identity_id"]),
        policy_epoch=int(identity["policy_epoch"]),
    )


def _runtime(monkeypatch, tmp_path, llm=None):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, llm or _GroundedLLM())
    return tools, storage


def test_owner_admin_cross_user_material_search_and_user_denial(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    material_info = {item.name: item for item in tools.list()}["materials.search"]
    assert material_info.default_presets == ["admin"]
    assert material_info.required_presets == ["owner", "admin"]
    writer = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-main",
        provider_subject_id="101",
        username="writer_cn",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-main",
        provider_subject_id="202",
        username="reader_admin",
        bootstrap_preset="admin",
    )

    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("项目讨论")
        message_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="秘密项目代号是青龙，明天继续讨论。",
        )
        storage.add_memory(content="비밀 프로젝트의 배포 일정은 금요일입니다.")
        source = tmp_path / "notes.txt"
        source.write_text("placeholder", encoding="utf-8")
        file_record = storage.create_file_record(
            name="秘密プロジェクト.txt",
            stored_path=source,
            sha256="a" * 64,
            size=source.stat().st_size,
            mime_type="text/plain",
            status="indexed",
            chunk_count=1,
        )
        storage.add_file_chunks(
            file_record["id"],
            ["秘密プロジェクトの責任者は田中です。"],
        )

        denied = asyncio.run(
            tools.run(
                "materials.search",
                {"query": "秘密项目", "all_users": True},
            )
        )
        assert denied.ok is False
        assert denied.data["authorization_denied"] is True

    owner_result = asyncio.run(
        tools.run(
            "materials.search",
            {
                "query": "秘密项目",
                "all_users": True,
                "source_types": ["messages"],
            },
        )
    )
    assert owner_result.ok is True
    assert owner_result.data["hits"][0]["source_id"] == message_id
    assert owner_result.data["hits"][0]["account"]["username"] == "writer_cn"

    with bind_actor(_actor(admin)):
        admin_result = asyncio.run(
            tools.run(
                "materials.search",
                {
                    "query": "秘密プロジェクト",
                    "username": "writer_cn",
                    "source_types": ["documents"],
                },
            )
        )
        assert admin_result.ok is True
        document_hit = admin_result.data["hits"][0]
        assert document_hit["file_id"] == file_record["id"]
        assert document_hit["source_id"] == file_record["id"]
        assert document_hit["chunk_id"]
        assert "stored_path" not in document_hit

        read_result = asyncio.run(
            tools.run(
                "materials.read",
                {
                    "source_type": "document",
                    "source_id": document_hit["source_id"],
                    "username": "writer_cn",
                },
            )
        )
        assert read_result.ok is True
        assert "田中" in read_result.data["content"]
        assert "stored_path" not in read_result.data

    with storage.locked_connection() as conn:
        audits = conn.execute(
            "SELECT query_sha256, details_json FROM material_access_audit"
        ).fetchall()
    assert audits
    assert all("秘密项目" not in str(dict(row)) for row in audits)
    storage.close()


def test_material_scoring_does_not_hold_storage_mutation_lock(monkeypatch, tmp_path):
    import jarvis_gpt.material_access as material_access_module
    from jarvis_gpt.authorization import current_actor

    tools, storage = _runtime(monkeypatch, tmp_path)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="material-concurrency",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    writer_actor = _actor(writer)
    with bind_actor(writer_actor):
        conversation_id = storage.create_conversation("Concurrent search")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="blocking sentinel inside another tenant material",
        )

    scoring_started = threading.Event()
    release_scoring = threading.Event()
    search_done = threading.Event()
    writer_done = threading.Event()
    search_error: list[BaseException] = []
    owner_actor = current_actor()
    original_match_score = material_access_module._match_score

    def blocking_match_score(text, queries):
        scoring_started.set()
        if not release_scoring.wait(timeout=5):
            raise TimeoutError("test did not release material scoring")
        return original_match_score(text, queries)

    monkeypatch.setattr(material_access_module, "_match_score", blocking_match_score)

    def search_worker() -> None:
        try:
            tools.material_access.search(
                owner_actor,
                queries=["blocking sentinel"],
                target_user_ids=[str(writer["user_id"])],
                source_types=["messages"],
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            search_error.append(exc)
        finally:
            search_done.set()

    def write_worker() -> None:
        with bind_actor(writer_actor):
            storage.add_message(
                conversation_id=conversation_id,
                role="user",
                content="writer proceeds while material scoring is paused",
            )
        writer_done.set()

    search_thread = threading.Thread(target=search_worker)
    search_thread.start()
    assert scoring_started.wait(timeout=3)
    write_thread = threading.Thread(target=write_worker)
    write_thread.start()
    try:
        assert writer_done.wait(timeout=2), "material scoring held the storage write lock"
    finally:
        release_scoring.set()
    search_thread.join(timeout=5)
    write_thread.join(timeout=5)

    assert search_done.is_set()
    assert search_error == []
    storage.close()


def test_material_service_rejects_forged_or_stale_privileged_actor(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    ordinary = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="material-floor",
        provider_subject_id="ordinary",
        bootstrap_preset="user",
    )
    forged = ActorContext(
        user_id=str(ordinary["user_id"]),
        preset_key="admin",
        source="forged-test",
        identity_id=str(ordinary["identity_id"]),
        policy_epoch=int(ordinary["policy_epoch"]),
    )

    with pytest.raises(MaterialAccessDeniedError):
        tools.material_access.accounts(forged)
    storage.close()


def test_account_resolution_fails_closed_and_multilingual_summary(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    first = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-one",
        provider_subject_id="301",
        username="duplicate_name",
        bootstrap_preset="user",
    )
    tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-two",
        provider_subject_id="302",
        username="duplicate_name",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(first)):
        conversation_id = storage.create_conversation("Project")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="The secret project launch is Friday.",
        )

    ambiguous = asyncio.run(
        tools.run(
            "materials.search",
            {"query": "secret project", "username": "duplicate_name"},
        )
    )
    assert ambiguous.ok is False
    assert "not unique" in ambiguous.summary

    exact = asyncio.run(
        tools.run(
            "materials.summarize",
            {
                "query": "секретный проект",
                "languages": "all",
                "provider": "telegram",
                "realm_id": "bot-one",
                "provider_subject_id": "301",
                "source_types": ["messages"],
            },
        )
    )
    assert exact.ok is True
    assert exact.data["sources"]
    assert exact.data["search"]["queries"] == [
        "секретный проект",
        "secret project",
        "秘密项目",
        "비밀 프로젝트",
        "秘密プロジェクト",
    ]
    storage.close()


class _SemanticBackend:
    remote_enabled = True

    async def embed(self, texts):
        def vector(text: str) -> list[float]:
            folded = text.casefold()
            if "release" in folded or "deployment window" in folded:
                return [1.0, 0.0, 0.0]
            if "subscribers leaving" in folded or "customer attrition" in folded:
                return [0.0, 1.0, 0.0]
            if "reactor temperature" in folded or "heat exchanger" in folded:
                return [0.0, 0.0, 1.0]
            return [0.0, 0.0, 0.0]

        return [vector(str(text)) for text in texts]


class _UnavailableSemanticBackend:
    remote_enabled = True

    async def embed(self, _texts):
        raise RuntimeError("embedding endpoint unavailable")


class _PagedSemanticBackend(_SemanticBackend):
    def __init__(self, *, on_document_page=None):
        self.batch_sizes: list[int] = []
        self.on_document_page = on_document_page

    async def embed(self, texts):
        self.batch_sizes.append(len(texts))
        if len(self.batch_sizes) > 1 and self.on_document_page is not None:
            callback, self.on_document_page = self.on_document_page, None
            callback()
        return await super().embed(texts)


def test_semantic_paraphrase_retrieval_covers_every_material_source(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="semantic-materials",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Semantic evidence")
        message_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="The deployment window closes Friday.",
        )
        memory = storage.add_memory(content="Customer attrition rose after the billing change.")
        source = tmp_path / "thermal.txt"
        source.write_text("placeholder", encoding="utf-8")
        file_record = storage.create_file_record(
            name="thermal-notes.txt",
            stored_path=source,
            sha256="b" * 64,
            size=source.stat().st_size,
            mime_type="text/plain",
            status="indexed",
            chunk_count=1,
        )
        storage.add_file_chunks(
            str(file_record["id"]),
            ["The heat exchanger is fouled and needs cleaning."],
        )

    cases = (
        ("When should we release?", "messages", message_id),
        ("Why are subscribers leaving?", "memories", str(memory["id"])),
        ("What caused the reactor temperature problem?", "documents", file_record["id"]),
    )
    for query, source_type, expected_id in cases:
        result = asyncio.run(
            tools.material_access.search_semantic(
                current_actor(),
                queries=[query],
                target_user_ids=[str(writer["user_id"])],
                embedding_backend=_SemanticBackend(),
                source_types=[source_type],
                limit=5,
            )
        )
        assert result["hits"][0]["source_id"] == expected_id
        assert result["hits"][0]["retrieval"] == "hybrid_embeddings"
    storage.close()


def test_semantic_search_pages_the_complete_old_message_corpus(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="semantic-full-corpus",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Long semantic archive")
        target_message_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="The deployment window closes Friday.",
        )
        for index in range(450):
            storage.add_message(
                conversation_id=conversation_id,
                role="user",
                content=f"Unrelated later archive row {index}",
            )

    statements: list[str] = []
    original_open = tools.material_access._open_search_connection

    def traced_connection():
        connection = original_open()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(tools.material_access, "_open_search_connection", traced_connection)
    backend = _PagedSemanticBackend()
    result = asyncio.run(
        tools.material_access.search_semantic(
            current_actor(),
            queries=["When should we release?"],
            target_user_ids=[str(writer["user_id"])],
            embedding_backend=backend,
            source_types=["messages"],
            limit=5,
        )
    )

    assert result["hits"][0]["source_id"] == target_message_id
    assert result["corpus_scan"] == {
        "complete": True,
        "rows_scanned": 451,
        "page_size": 64,
    }
    assert len(backend.batch_sizes) >= 9  # one query batch plus eight corpus pages
    assert max(backend.batch_sizes[1:]) <= 64
    normalized = [statement.strip().upper() for statement in statements]
    assert normalized.count("BEGIN") == 1
    assert normalized.count("COMMIT") == 1
    storage.close()


def test_semantic_search_rechecks_admin_after_page_embedding(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="semantic-live-recheck",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="semantic-live-recheck",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Revocation corpus")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="The deployment window closes Friday.",
        )

    def demote_admin() -> None:
        tools.permissions.assign_preset(
            user_id=str(admin["user_id"]),
            preset_key="user",
            assigned_by=LEGACY_OWNER_USER_ID,
            reason="test semantic page revocation",
        )

    backend = _PagedSemanticBackend(on_document_page=demote_admin)
    with bind_actor(_actor(admin)), pytest.raises(MaterialAccessDeniedError):
        asyncio.run(
            tools.material_access.search_semantic(
                current_actor(),
                queries=["When should we release?"],
                target_user_ids=[str(writer["user_id"])],
                embedding_backend=backend,
                source_types=["messages"],
                limit=5,
            )
        )
    storage.close()


def test_many_user_search_is_fair_strict_and_repeatable(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    noisy = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="fair-materials",
        provider_subject_id="noisy",
        bootstrap_preset="user",
    )
    target = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="fair-materials",
        provider_subject_id="target",
        bootstrap_preset="user",
    )
    excluded = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="fair-materials",
        provider_subject_id="excluded",
        bootstrap_preset="user",
    )
    additional_users: list[dict[str, object]] = []
    for index in range(12):
        identity = tools.permissions.upsert_external_identity(
            provider="test",
            realm_id="fair-materials",
            provider_subject_id=f"additional-{index}",
            bootstrap_preset="user",
        )
        additional_users.append(identity)
        with bind_actor(_actor(identity)):
            conversation_id = storage.create_conversation(f"Additional {index}")
            storage.add_message(
                conversation_id=conversation_id,
                role="user",
                content=f"needle secondary tenant {index}",
            )
    with bind_actor(_actor(noisy)):
        conversation_id = storage.create_conversation("Noisy corpus")
        for index in range(90):
            storage.add_message(
                conversation_id=conversation_id,
                role="user",
                content=f"needle filler record {index}",
            )
    with bind_actor(_actor(target)):
        conversation_id = storage.create_conversation("Target corpus")
        target_message_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="unique needle conclusion belongs to the target tenant",
        )
    with bind_actor(_actor(excluded)):
        conversation_id = storage.create_conversation("Excluded corpus")
        excluded_message_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="unique needle conclusion is an even stronger excluded result",
        )

    kwargs = {
        "queries": ["unique needle conclusion"],
        "target_user_ids": [
            str(noisy["user_id"]),
            *(str(identity["user_id"]) for identity in additional_users),
            str(target["user_id"]),
        ],
        "source_types": ["messages"],
        "limit": 5,
    }
    first = tools.material_access.search(current_actor(), **kwargs)
    second = tools.material_access.search(current_actor(), **kwargs)
    first_ids = [hit["source_id"] for hit in first["hits"]]
    assert target_message_id in first_ids
    assert excluded_message_id not in first_ids
    assert first_ids == [hit["source_id"] for hit in second["hits"]]

    fallback_first = asyncio.run(
        tools.material_access.search_semantic(
            current_actor(),
            embedding_backend=_UnavailableSemanticBackend(),
            **kwargs,
        )
    )
    fallback_second = asyncio.run(
        tools.material_access.search_semantic(
            current_actor(),
            embedding_backend=_UnavailableSemanticBackend(),
            **kwargs,
        )
    )
    assert fallback_first["retrieval_mode"] == "hybrid_local_fallback"
    assert fallback_first["hits"] == fallback_second["hits"]
    storage.close()


def test_material_search_uses_one_explicit_read_snapshot(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="snapshot-materials",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Snapshot")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="snapshot marker",
        )
        storage.add_memory(content="snapshot marker memory")

    statements: list[str] = []
    original_open = tools.material_access._open_search_connection

    def traced_connection():
        connection = original_open()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(
        tools.material_access,
        "_open_search_connection",
        traced_connection,
    )
    result = tools.material_access.search(
        current_actor(),
        queries=["snapshot marker"],
        target_user_ids=[str(writer["user_id"])],
        source_types=["messages", "memories"],
    )
    normalized = [statement.strip().upper() for statement in statements]
    assert result["count"] >= 2
    assert normalized.count("BEGIN") == 1
    assert normalized.count("COMMIT") == 1
    storage.close()


def test_document_read_counts_separators_and_exact_truncation(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="document-truncation",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        source = tmp_path / "truncated.txt"
        source.write_text("placeholder", encoding="utf-8")
        truncated_file = storage.create_file_record(
            name="truncated.txt",
            stored_path=source,
            sha256="c" * 64,
            size=source.stat().st_size,
            mime_type="text/plain",
            status="indexed",
            chunk_count=2,
        )
        storage.add_file_chunks(str(truncated_file["id"]), ["a" * 600, "b" * 500])
        exact_file = storage.create_file_record(
            name="exact.txt",
            stored_path=source,
            sha256="d" * 64,
            size=source.stat().st_size,
            mime_type="text/plain",
            status="indexed",
            chunk_count=2,
        )
        storage.add_file_chunks(str(exact_file["id"]), ["a" * 600, "b" * 398])

    truncated = tools.material_access.read(
        current_actor(),
        source_type="document",
        source_id=str(truncated_file["id"]),
        target_user_ids=[str(writer["user_id"])],
        max_chars=1_000,
    )
    assert len(truncated["content"]) == 1_000
    assert truncated["content"][600:602] == "\n\n"
    assert truncated["content_chars_total"] == 1_102
    assert truncated["chunks_returned"] == 2
    assert truncated["content_truncated"] is True

    exact = tools.material_access.read(
        current_actor(),
        source_type="document",
        source_id=str(exact_file["id"]),
        target_user_ids=[str(writer["user_id"])],
        max_chars=1_000,
    )
    assert len(exact["content"]) == 1_000
    assert exact["content_chars_total"] == 1_000
    assert exact["chunks_returned"] == 2
    assert exact["content_truncated"] is False
    storage.close()


def test_document_search_read_and_summary_expose_sanitized_partial_ocr_index(monkeypatch, tmp_path):
    inspecting_llm = _SummaryInspectionLLM()
    tools, storage = _runtime(monkeypatch, tmp_path, inspecting_llm)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="partial-ocr-index",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        source = tmp_path / "partial-scan.pdf"
        source.write_bytes(b"placeholder")
        file_record = storage.create_file_record(
            name="partial-scan.pdf",
            stored_path=source,
            sha256="e" * 64,
            size=source.stat().st_size,
            mime_type="application/pdf",
            status="stored",
            chunk_count=0,
        )
        storage.persist_file_extracted_text(
            str(file_record["id"]),
            "OCR_PARTIAL_SENTINEL recognized on the first pages.",
            source="vlm_ocr:qwen36-vl",
            details={
                "pages_total": 100,
                "pages_attempted": 30,
                "pages_recognized": 29,
                "pages_failed": 1,
                "pages_truncated": 70,
                "characters_recognized": 250_000,
                "characters_indexed": 200_000,
                "text_truncated": True,
                "automatic": True,
                "stored_path": "C:\\private\\source.pdf",
                "secret": "DO_NOT_EXPOSE",
            },
            warning="OCR warning at C:\\private\\source.pdf with DO_NOT_EXPOSE",
        )

    searched = tools.material_access.search(
        current_actor(),
        queries=["OCR_PARTIAL_SENTINEL"],
        target_user_ids=[str(writer["user_id"])],
        source_types=["documents"],
        limit=5,
    )
    index = searched["hits"][0]["index"]
    assert index["state"] == "partial"
    assert index["complete"] is False
    assert index["source"] == "vlm_ocr:qwen36-vl"
    assert index["details"]["pages_total"] == 100
    assert index["details"]["pages_truncated"] == 70
    assert index["details"]["text_truncated"] is True
    assert len(index["warnings"]) == 3

    read = tools.material_access.read(
        current_actor(),
        source_type="document",
        source_id=str(file_record["id"]),
        target_user_ids=[str(writer["user_id"])],
    )
    assert read["content_truncated"] is False
    assert read["index"] == index

    summarized = asyncio.run(
        tools.run(
            "materials.summarize",
            {
                "query": "OCR_PARTIAL_SENTINEL",
                "user_id": str(writer["user_id"]),
                "source_types": ["documents"],
            },
        )
    )
    assert summarized.ok is True
    summary_index = summarized.data["search"]["hits"][0]["index"]
    assert summary_index["state"] == "partial"
    llm_index = inspecting_llm.evidence_payloads[0]["evidence"][0]["index"]
    assert llm_index == index
    serialized = json.dumps(
        {"search": searched, "read": read, "summary": summarized.data},
        ensure_ascii=False,
    )
    assert "C:\\private" not in serialized
    assert "DO_NOT_EXPOSE" not in serialized
    assert "stored_path" not in serialized
    storage.close()


class _TranslationFailureLLM:
    async def complete(self, messages, **_kwargs):
        text = str(messages[-1].get("content") or "")
        if '"languages"' in text:
            return SimpleNamespace(ok=False, content="")
        return SimpleNamespace(ok=False, content="")


def test_material_search_exposes_translation_failure(monkeypatch, tmp_path):
    tools, storage = _runtime(monkeypatch, tmp_path, _TranslationFailureLLM())
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="translation-coverage",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Coverage")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="секретный проект существует",
        )
    result = asyncio.run(
        tools.run(
            "materials.search",
            {
                "query": "секретный проект",
                "user_id": str(writer["user_id"]),
                "source_types": ["messages"],
            },
        )
    )
    assert result.ok is True
    coverage = result.data["language_coverage"]
    assert coverage["translation_complete"] is False
    assert coverage["untranslated_languages"] == ["ru", "en", "zh", "ko", "ja"]
    storage.close()


class _SummaryInspectionLLM(_GroundedLLM):
    def __init__(self, *, uncited: bool = False, demote=None):
        self.evidence_payloads: list[dict[str, object]] = []
        self.uncited = uncited
        self.demote = demote

    async def complete(self, messages, **_kwargs):
        text = str(messages[-1].get("content") or "")
        if '"languages"' in text:
            return await super().complete(messages, **_kwargs)
        payload = json.loads(text)
        evidence = payload.get("evidence") if isinstance(payload, dict) else []
        if isinstance(evidence, list) and evidence:
            self.evidence_payloads.append(payload)
            if self.demote is not None:
                callback, self.demote = self.demote, None
                callback()
            citation = str(evidence[0].get("citation") or "")
        else:
            allowed = payload.get("allowed_citations") if isinstance(payload, dict) else []
            citation = str(allowed[0]) if isinstance(allowed, list) and allowed else ""
        if self.uncited:
            content = f"Подтверждённый вывод [{citation}].\n\nВывод без ссылки."
        else:
            content = f"Найден итоговый вывод [{citation}]."
        return SimpleNamespace(ok=True, content=content)


def test_summary_reads_late_message_content_and_rejects_uncited_claims(monkeypatch, tmp_path):
    inspecting_llm = _SummaryInspectionLLM()
    tools, storage = _runtime(monkeypatch, tmp_path, inspecting_llm)
    writer = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="summary-read",
        provider_subject_id="writer",
        username="summary_writer",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Long evidence")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=(
                "archive sentinel "
                + "background " * 180
                + "FINAL_CONCLUSION_AFTER_NINE_HUNDRED_CHARS"
            ),
        )
    result = asyncio.run(
        tools.run(
            "materials.summarize",
            {
                "query": "archive sentinel",
                "username": "summary_writer",
                "source_types": ["messages"],
            },
        )
    )
    assert result.ok is True
    evidence = inspecting_llm.evidence_payloads[0]["evidence"]
    assert "FINAL_CONCLUSION_AFTER_NINE_HUNDRED_CHARS" in evidence[0]["content"]

    uncited_llm = _SummaryInspectionLLM(uncited=True)
    tools.llm = uncited_llm
    denied = asyncio.run(
        tools.run(
            "materials.summarize",
            {
                "query": "archive sentinel",
                "username": "summary_writer",
                "source_types": ["messages"],
            },
        )
    )
    assert denied.ok is True
    assert denied.data["synthesis_mode"] == "deterministic_evidence_digest"
    assert "Вывод без ссылки" not in denied.data["summary"]
    assert "FINAL_CONCLUSION_AFTER_NINE_HUNDRED_CHARS" in denied.data["summary"]
    citations = {source["citation"] for source in denied.data["sources"]}
    assert citations
    assert all(f"[{citation}]" in denied.data["summary"] for citation in citations)
    storage.close()


def test_summary_fails_closed_when_admin_is_demoted_during_synthesis(monkeypatch, tmp_path):
    llm = _SummaryInspectionLLM()
    tools, storage = _runtime(monkeypatch, tmp_path, llm)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="summary-revocation",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="summary-revocation",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Revocation evidence")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="revocation sentinel conclusion",
        )

    def demote_admin() -> None:
        tools.permissions.assign_preset(
            user_id=str(admin["user_id"]),
            preset_key="user",
            assigned_by=LEGACY_OWNER_USER_ID,
            reason="test live material synthesis revocation",
        )

    llm.demote = demote_admin
    with bind_actor(_actor(admin)):
        result = asyncio.run(
            tools.run(
                "materials.summarize",
                {
                    "query": "revocation sentinel",
                    "user_id": str(writer["user_id"]),
                    "source_types": ["messages"],
                },
            )
        )
    assert result.ok is False
    assert result.data["authorization_denied"] is True
    assert result.data["result_withheld"] is True
    storage.close()
