from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import jarvis_gpt.tools as tools_module
import pytest
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    bind_actor,
    current_actor,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.material_access import (
    MaterialAccessDeniedError,
    MaterialTargetNotFoundError,
)
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolContext, ToolRegistry


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


def test_recent_messages_are_exact_role_scoped_and_deterministically_ordered(
    monkeypatch, tmp_path
):
    tools, storage = _runtime(monkeypatch, tmp_path)
    recent_info = {item.name: item for item in tools.list()}["materials.recent"]
    assert recent_info.required_presets == ["owner", "admin"]
    writer = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-main",
        provider_subject_id="recent-writer",
        username="JBL61R",
        bootstrap_preset="user",
    )
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Recent messages")
        first_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="Первое пользовательское сообщение.",
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content="Промежуточный ответ Jarvis.",
        )
        second_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="Второе пользовательское сообщение.",
        )
        third_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="Третье пользовательское сообщение.",
        )
        denied = asyncio.run(
            tools.run(
                "materials.recent",
                {"username": "@JBL61R", "limit": 2},
            )
        )
        assert denied.ok is False
        assert denied.data["authorization_denied"] is True

    newest = asyncio.run(
        tools.run(
            "materials.recent",
            {"username": "@JBL61R", "limit": 2},
        )
    )
    assert newest.ok is True
    assert newest.data["display_order"] == "newest_first"
    assert newest.data["roles"] == ["user"]
    assert [item["source_id"] for item in newest.data["messages"]] == [
        third_id,
        second_id,
    ]
    assert [item["citation"] for item in newest.data["messages"]] == [
        f"message:{third_id}",
        f"message:{second_id}",
    ]
    assert all(item["account"]["username"] == "JBL61R" for item in newest.data["messages"])

    chronological = asyncio.run(
        tools.run(
            "materials.recent",
            {
                "username": "JBL61R",
                "limit": 3,
                "order": "oldest_first",
            },
        )
    )
    assert [item["source_id"] for item in chronological.data["messages"]] == [
        first_id,
        second_id,
        third_id,
    ]
    missing = asyncio.run(
        tools.run("materials.recent", {"username": "missing_recent_user", "limit": 2})
    )
    assert missing.ok is False
    assert "No account matches" in missing.summary

    tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-secondary",
        provider_subject_id="recent-writer-duplicate",
        username="JBL61R",
        bootstrap_preset="user",
    )
    ambiguous = asyncio.run(
        tools.run("materials.recent", {"username": "JBL61R", "limit": 2})
    )
    assert ambiguous.ok is False
    assert "not unique" in ambiguous.summary
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


def test_exact_formal_name_date_summary_filters_audio_and_cites_documents(
    monkeypatch,
    tmp_path,
):
    tools, storage = _runtime(monkeypatch, tmp_path)
    target = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-main",
        provider_subject_id="formal-target",
        username="hamelion55k",
        first_name="Хамелион",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="formal-reader",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )

    with bind_actor(_actor(target)):
        document_path = tmp_path / "report.docx"
        document_path.write_bytes(b"document fixture")
        document = storage.create_file_record(
            name=document_path.name,
            stored_path=document_path,
            sha256="d" * 64,
            size=document_path.stat().st_size,
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            status="indexed",
            chunk_count=1,
        )
        storage.add_file_chunks(
            document["id"],
            ["SHARED_DATE_SENTINEL verified document contents."],
        )

        voice_path = tmp_path / "voice.ogg"
        voice_path.write_bytes(b"voice fixture")
        voice = storage.create_file_record(
            name=voice_path.name,
            stored_path=voice_path,
            sha256="e" * 64,
            size=voice_path.stat().st_size,
            mime_type="audio/ogg",
            status="indexed",
            chunk_count=1,
        )
        storage.add_file_chunks(
            voice["id"],
            ["SHARED_DATE_SENTINEL must never be document evidence."],
        )

    admin_actor = _actor(admin)
    with bind_actor(admin_actor):
        summarized = asyncio.run(
            tools.run(
                "materials.summarize",
                {
                    "query": "Дай краткую выжимку документов.",
                    "account_name": "  ХАМЕЛИОН  ",
                    "source_types": ["documents"],
                    "date_from": "2000-01-01T00:00:00+00:00",
                    "date_to": "2100-01-01T00:00:00+00:00",
                    "max_hits": 10,
                },
            )
        )
        searched = tools.material_access.search(
            admin_actor,
            queries=["SHARED_DATE_SENTINEL"],
            target_user_ids=[str(target["user_id"])],
            source_types=["documents"],
            limit=10,
        )
        with pytest.raises(MaterialTargetNotFoundError):
            tools.material_access.read(
                admin_actor,
                source_type="document",
                source_id=str(voice["id"]),
                target_user_ids=[str(target["user_id"])],
            )

    assert summarized.ok is True
    assert summarized.data["search"]["count"] == 1
    assert summarized.data["sources"] == [
        {
            "citation": f"document:{document['id']}",
            "account": summarized.data["sources"][0]["account"],
            "created_at": summarized.data["sources"][0]["created_at"],
            "source_type": "document",
            "file_name": "report.docx",
        }
    ]
    assert f"[document:{document['id']}]" in summarized.data["summary"]
    assert str(voice["id"]) not in json.dumps(summarized.data, ensure_ascii=False)
    assert {item["source_id"] for item in searched["hits"]} == {document["id"]}

    duplicate = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="bot-secondary",
        provider_subject_id="formal-duplicate",
        username="another_formal_target",
        first_name="Хамелион",
        bootstrap_preset="user",
    )
    assert duplicate["user_id"] != target["user_id"]
    with bind_actor(admin_actor):
        ambiguous = asyncio.run(
            tools.run(
                "materials.summarize",
                {
                    "query": "Дай краткую выжимку документов.",
                    "account_name": "Хамелион",
                    "source_types": ["documents"],
                    "date_from": "2000-01-01T00:00:00+00:00",
                    "date_to": "2100-01-01T00:00:00+00:00",
                },
            )
        )
        missing = asyncio.run(
            tools.run(
                "materials.summarize",
                {
                    "query": "Дай краткую выжимку документов.",
                    "account_name": "Хамел",
                    "source_types": ["documents"],
                    "date_from": "2000-01-01T00:00:00+00:00",
                    "date_to": "2100-01-01T00:00:00+00:00",
                },
            )
        )

    assert ambiguous.ok is False
    assert "not unique" in ambiguous.summary
    assert missing.ok is False
    assert "No account matches" in missing.summary
    storage.close()


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


class _DateDigestContractLLM(_GroundedLLM):
    def __init__(self, *, valid_correction: bool):
        self.valid_correction = valid_correction
        self.calls: list[dict[str, object]] = []
        self.max_tokens: list[int] = []

    async def complete(self, messages, **kwargs):
        payload = json.loads(str(messages[-1].get("content") or "{}"))
        self.calls.append(payload)
        self.max_tokens.append(int(kwargs.get("max_tokens") or 0))
        evidence = payload.get("evidence") if isinstance(payload, dict) else []
        if not isinstance(evidence, list) or not evidence:
            raise AssertionError("date digest requires document evidence")
        if "draft" not in payload:
            bullets = [
                (
                    f"- {item.get('file_name')}: "
                    f"{'OVERLONG_DRAFT_SENTINEL ' * 60}"
                    f"[{item.get('citation')}]"
                )
                for item in evidence
            ]
            citation = str(evidence[0].get("citation") or "")
            return SimpleNamespace(
                ok=True,
                content=(
                    "\n".join(bullets)
                    + f"\n\nOverlong overall conclusion. [{citation}]"
                ),
            )
        if not self.valid_correction:
            return SimpleNamespace(
                ok=True,
                content="INVALID_OVERLONG_CORRECTION " * 300,
            )
        bullets = [
            (
                f"- {item.get('file_name')}: compact verified point "
                f"[{item.get('citation')}]"
            )
            for item in evidence
        ]
        citation = str(evidence[0].get("citation") or "")
        return SimpleNamespace(
            ok=True,
            content=(
                "\n".join(bullets)
                + "\n\nTen documents were processed. The digest is intentionally compact. "
                f"[{citation}]"
            ),
        )


class _PostAwaitRevocationLLM(_GroundedLLM):
    def __init__(self, mode: str):
        self.mode = mode
        self.demote = None
        self.synthesis_calls = 0
        self.correction_calls = 0
        self.evidence_payloads: list[dict[str, object]] = []

    def _demote_once(self) -> None:
        if self.demote is None:
            raise AssertionError("demotion callback was not configured")
        callback, self.demote = self.demote, None
        callback()

    async def complete(self, messages, **_kwargs):
        text = str(messages[-1].get("content") or "")
        if '"languages"' in text:
            return await super().complete(messages, **_kwargs)
        payload = json.loads(text)
        if "allowed_citations" in payload:
            self.correction_calls += 1
            if self.mode != "invalid_correction":
                raise AssertionError("unexpected citation-correction request")
            self._demote_once()
            return SimpleNamespace(
                ok=True,
                content="Invalid corrected draft without a citation.",
            )

        self.synthesis_calls += 1
        evidence = payload.get("evidence") if isinstance(payload, dict) else []
        if not isinstance(evidence, list) or not evidence:
            raise AssertionError("summary evidence was not supplied")
        self.evidence_payloads.append(payload)
        if self.mode == "empty_synthesis":
            self._demote_once()
            return SimpleNamespace(ok=False, content="")
        if self.mode == "invalid_correction":
            return SimpleNamespace(ok=True, content="Uncited synthesis draft.")
        raise AssertionError(f"unsupported revocation mode: {self.mode}")


class _ForbiddenSynthesisLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, _messages, **_kwargs):
        self.calls += 1
        raise AssertionError("revoked cross-user evidence must not reach the model")


def _direct_material_context(tools: ToolRegistry, actor: ActorContext) -> ToolContext:
    return ToolContext(
        settings=tools.settings,
        storage=tools.storage,
        llm=tools.llm,
        execution=tools.execution,
        verifier=tools.verifier,
        safe_gate=tools.safe_gate,
        actor=actor,
        material_access=tools.material_access,
        embeddings=tools.embeddings,
    )


def test_date_document_digest_binds_each_filename_to_its_own_citation():
    evidence = [
        {"citation": "document:file-a", "file_name": "Alpha Report.docx"},
        {"citation": "document:file-b", "file_name": "Beta Report.docx"},
    ]
    correct = (
        "- Alpha Report.docx: first point [document:file-a]\n"
        "- Beta Report.docx: second point [document:file-b]\n\n"
        "Two documents were reviewed. [document:file-a]"
    )
    omitted = (
        "- First point [document:file-a]\n"
        "- Second point [document:file-b]\n\n"
        "Two documents were reviewed. [document:file-a]"
    )
    swapped = (
        "- Beta Report.docx: first point [document:file-a]\n"
        "- Alpha Report.docx: second point [document:file-b]\n\n"
        "Two documents were reviewed. [document:file-a]"
    )

    assert tools_module._date_document_digest_is_valid(
        correct,
        evidence,
        max_chars=4000,
    )
    assert not tools_module._date_document_digest_is_valid(
        omitted,
        evidence,
        max_chars=4000,
    )
    assert not tools_module._date_document_digest_is_valid(
        swapped,
        evidence,
        max_chars=4000,
    )


@pytest.mark.parametrize(
    ("valid_correction", "expected_mode"),
    [
        (True, "llm_corrected"),
        (False, "deterministic_evidence_digest"),
    ],
)
def test_date_document_digest_rewrites_overlong_answer_without_losing_sources(
    monkeypatch,
    tmp_path,
    valid_correction,
    expected_mode,
):
    llm = _DateDigestContractLLM(valid_correction=valid_correction)
    tools, storage = _runtime(monkeypatch, tmp_path, llm)
    target = tools.permissions.upsert_external_identity(
        provider="telegram",
        realm_id="date-digest-contract",
        provider_subject_id="writer",
        username="date_digest_writer",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="date-digest-contract",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )
    documents: dict[str, str] = {}
    with bind_actor(_actor(target)):
        for index in range(10):
            path = tmp_path / f"daily-report-{index + 1:02d}.docx"
            path.write_bytes(f"document fixture {index}".encode())
            document = storage.create_file_record(
                name=path.name,
                stored_path=path,
                sha256=f"{index + 1:064x}",
                size=path.stat().st_size,
                mime_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                status="indexed",
                chunk_count=1,
            )
            storage.add_file_chunks(
                document["id"],
                [f"Daily verified point {index + 1}."],
            )
            documents[str(document["id"])] = path.name

    with bind_actor(_actor(admin)):
        result = asyncio.run(
            tools.run(
                "materials.summarize",
                {
                    "query": "Дай краткую выжимку документов за сегодня.",
                    "username": "date_digest_writer",
                    "source_types": ["documents"],
                    "date_from": "2000-01-01T00:00:00+00:00",
                    "date_to": "2100-01-01T00:00:00+00:00",
                    "max_hits": 60,
                    "max_tokens": 1400,
                },
            )
        )

    assert result.ok is True
    assert result.data["synthesis_mode"] == expected_mode
    assert result.data["digest_contract"] == {
        "max_chars": 4000,
        "max_tokens": 800,
        "one_bullet_per_document": True,
        "source_count": 10,
    }
    summary = str(result.data["summary"])
    assert len(summary) <= 4000
    assert "OVERLONG_DRAFT_SENTINEL" not in summary
    assert "INVALID_OVERLONG_CORRECTION" not in summary
    bullets = [line for line in summary.splitlines() if line.startswith("- ")]
    assert len(bullets) == 10
    for document_id, file_name in documents.items():
        assert any(
            file_name in line and f"[document:{document_id}]" in line
            for line in bullets
        )
    assert len(llm.calls) == 2
    assert llm.max_tokens == [800, 800]
    storage.close()


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


@pytest.mark.parametrize(
    ("mode", "expected_correction_calls"),
    [
        ("empty_synthesis", 0),
        ("invalid_correction", 1),
    ],
)
def test_summary_post_await_failure_hides_evidence_after_admin_demotion(
    monkeypatch,
    tmp_path,
    mode,
    expected_correction_calls,
):
    llm = _PostAwaitRevocationLLM(mode)
    tools, storage = _runtime(monkeypatch, tmp_path, llm)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id=f"summary-post-await-{mode}",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id=f"summary-post-await-{mode}",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )
    sentinel = f"POST_AWAIT_PRIVATE_EVIDENCE_{mode.upper()}"
    with bind_actor(_actor(writer)):
        conversation_id = storage.create_conversation("Post-await revocation evidence")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=sentinel,
        )

    def demote_admin() -> None:
        tools.permissions.assign_preset(
            user_id=str(admin["user_id"]),
            preset_key="user",
            assigned_by=LEGACY_OWNER_USER_ID,
            reason=f"test {mode} material synthesis revocation",
        )

    llm.demote = demote_admin
    if mode == "invalid_correction":
        monkeypatch.setattr(
            tools_module,
            "_deterministic_material_digest",
            lambda *_args, **_kwargs: "Invalid digest without a citation.",
        )

    spec = tools.get("materials.summarize")
    assert spec is not None
    admin_actor = _actor(admin)
    with bind_actor(admin_actor):
        result = asyncio.run(
            spec.handler(
                _direct_material_context(tools, admin_actor),
                {
                    "query": sentinel,
                    "user_id": str(writer["user_id"]),
                    "source_types": ["messages"],
                },
            )
        )

    serialized = json.dumps(
        {"summary": result.summary, "data": result.data},
        ensure_ascii=False,
    )
    assert result.ok is False
    assert result.data == {}
    assert sentinel not in serialized
    assert '"search"' not in serialized
    assert '"evidence"' not in serialized
    assert llm.synthesis_calls == 1
    assert llm.correction_calls == expected_correction_calls
    assert sentinel in json.dumps(llm.evidence_payloads, ensure_ascii=False)
    live_admin = tools.permissions.get_user(str(admin["user_id"]))
    assert live_admin is not None
    assert live_admin["preset_key"] == "user"
    storage.close()


def test_summary_demotion_during_material_read_never_reaches_synthesis(
    monkeypatch,
    tmp_path,
):
    llm = _ForbiddenSynthesisLLM()
    tools, storage = _runtime(monkeypatch, tmp_path, llm)
    writer = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="summary-read-revocation",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="summary-read-revocation",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )
    sentinel = "READ_REVOCATION_PRIVATE_DOCUMENT_SENTINEL"
    with bind_actor(_actor(writer)):
        path = tmp_path / "revocation-report.docx"
        path.write_bytes(b"document fixture")
        document = storage.create_file_record(
            name=path.name,
            stored_path=path,
            sha256="f" * 64,
            size=path.stat().st_size,
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            status="indexed",
            chunk_count=1,
        )
        storage.add_file_chunks(document["id"], [sentinel])

    original_read = tools.material_access.read
    demoted = False

    def demoting_read(*args, **kwargs):
        nonlocal demoted
        if not demoted:
            demoted = True
            tools.permissions.assign_preset(
                user_id=str(admin["user_id"]),
                preset_key="user",
                assigned_by=LEGACY_OWNER_USER_ID,
                reason="test material read revocation",
            )
        return original_read(*args, **kwargs)

    monkeypatch.setattr(tools.material_access, "read", demoting_read)
    spec = tools.get("materials.summarize")
    assert spec is not None
    admin_actor = _actor(admin)
    with bind_actor(admin_actor):
        result = asyncio.run(
            spec.handler(
                _direct_material_context(tools, admin_actor),
                {
                    "query": "Summarize the selected daily documents.",
                    "user_id": str(writer["user_id"]),
                    "source_types": ["documents"],
                    "date_from": "2000-01-01T00:00:00+00:00",
                    "date_to": "2100-01-01T00:00:00+00:00",
                },
            )
        )

    serialized = json.dumps(
        {"summary": result.summary, "data": result.data},
        ensure_ascii=False,
    )
    assert demoted is True
    assert result.ok is False
    assert result.data == {}
    assert sentinel not in serialized
    assert '"search"' not in serialized
    assert '"evidence"' not in serialized
    assert llm.calls == 0
    live_admin = tools.permissions.get_user(str(admin["user_id"]))
    assert live_admin is not None
    assert live_admin["preset_key"] == "user"
    storage.close()
