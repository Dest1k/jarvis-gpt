from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import jarvis_gpt.storage as storage_module
import pytest
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    bind_actor,
)
from jarvis_gpt.config import PROFILES, ensure_runtime_dirs, load_settings
from jarvis_gpt.models import MemoryVaultResponse
from jarvis_gpt.storage import JarvisStorage, _recoverable_fts_error


class _FailingFtsConnection:
    def __init__(self, message: str) -> None:
        self.message = message

    def execute(self, _statement: str):
        raise sqlite3.OperationalError(self.message)


def test_settings_use_external_home(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_PROFILE", "qwen36-vl")

    settings = load_settings()
    ensure_runtime_dirs(settings)

    assert settings.home == tmp_path
    assert settings.database_path.parent.exists()
    assert settings.model_root == tmp_path / "models"
    assert settings.model_dir.name == "qwen3.6-35b-a3b-nvfp4"


def test_qwen_profile_preserves_certified_vllm_tuning():
    profile = PROFILES["qwen36-vl"]

    assert profile.model_dir_name == "qwen3.6-35b-a3b-nvfp4"
    assert profile.cpu_offload_gb == 0
    assert profile.gpu_memory_utilization == 0.90
    assert profile.max_model_len == 32768
    assert profile.eager_mode is False
    assert profile.kv_cache_dtype == "fp8"
    assert profile.max_num_seqs == 16
    assert profile.tokenizer_mode == "auto"
    assert profile.vision_capable is True
    assert profile.suppress_model_thinking is True
    assert profile.vllm_extra_args.language_model_only is False
    assert profile.vllm_extra_args.skip_mm_profiling is True
    assert profile.vllm_extra_args.mm_processor_cache_gb == 4.0
    assert profile.vllm_extra_args.max_num_batched_tokens == 4096


def test_storage_only_degrades_for_expected_fts_errors(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")

    assert _recoverable_fts_error(sqlite3.OperationalError("no such module: fts5"))
    assert storage._ensure_memory_fts(_FailingFtsConnection("no such module: fts5")) is False
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        storage._ensure_memory_fts(_FailingFtsConnection("database is locked"))


def test_legacy_telegram_update_ledger_migrates_replay_lease_columns(tmp_path):
    database_path = tmp_path / "state" / "jarvis.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE telegram_updates (
                realm_id TEXT NOT NULL,
                update_id INTEGER NOT NULL,
                user_id TEXT,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(realm_id, update_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO telegram_updates(
                realm_id, update_id, payload_sha256, status, received_at, updated_at
            ) VALUES ('legacy', 1, 'digest', 'completed', '2020-01-01', '2020-01-01')
            """
        )

    storage = JarvisStorage(database_path)
    storage.initialize()
    columns = {
        str(row[1]) for row in storage.connect().execute("PRAGMA table_info(telegram_updates)")
    }
    row = storage.connect().execute(
        """
        SELECT attempt_count, lease_token, last_error
        FROM telegram_updates WHERE realm_id = 'legacy' AND update_id = 1
        """
    ).fetchone()

    assert {"attempt_count", "lease_token", "last_error"} <= columns
    assert dict(row) == {"attempt_count": 1, "lease_token": None, "last_error": None}
    storage.close()


def test_legacy_personal_tables_backfill_owner_before_tenant_indexes(tmp_path):
    database_path = tmp_path / "state" / "jarvis.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations(id, title, created_at, updated_at)
            VALUES ('legacy-conversation', 'Legacy', '2020-01-01', '2020-01-01')
            """
        )

    storage = JarvisStorage(database_path)
    storage.initialize()

    row = storage.connect().execute(
        "SELECT user_id FROM conversations WHERE id = 'legacy-conversation'"
    ).fetchone()
    indexes = {
        str(item[1])
        for item in storage.connect().execute("PRAGMA index_list(conversations)")
    }
    assert row["user_id"] == LEGACY_OWNER_USER_ID
    assert "idx_conversations_user_updated" in indexes
    storage.close()


def test_per_user_memory_vault_handle_cache_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_module, "_MEMORY_VAULT_CACHE_MAX", 2)
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")

    first = storage._memory_vault_for("usr_first")
    storage._memory_vault_for("usr_second")
    storage._memory_vault_for("usr_third")

    assert len(storage._user_memory_vaults) == 2
    assert "usr_first" not in storage._user_memory_vaults
    assert first.root.name == "usr_first"


def test_storage_persists_mission(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    mission = storage.create_mission(
        title="Build runtime",
        goal="Create a local-first runtime",
        tasks=["Design", "Implement", "Verify"],
    )

    assert mission["title"] == "Build runtime"
    assert len(mission["tasks"]) == 3
    assert storage.counters()["missions"] == 1
    storage.close()


def test_storage_reserved_mission_id_is_idempotent(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    first = storage.create_mission(
        mission_id="mis_reserved123456",
        title="Reserved mission",
        goal="Create exactly one durable mission",
        tasks=["Plan", "Verify"],
    )
    repeated = storage.create_mission(
        mission_id="mis_reserved123456",
        title="Reserved mission",
        goal="Create exactly one durable mission",
        tasks=["Plan", "Verify"],
    )

    assert repeated == first
    assert len(storage.list_missions()) == 1
    assert len(first["tasks"]) == 2
    assert len(storage.list_audit(target_type="mission", target_id=first["id"])) == 1
    with pytest.raises(ValueError, match="different goal"):
        storage.create_mission(
            mission_id="mis_reserved123456",
            title="Collision",
            goal="Different goal",
            tasks=["Never"],
        )
    storage.close()


def test_mission_creation_rolls_back_every_row_after_task_insert_failure(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conn = storage.connect()
    conn.execute(
        """
        CREATE TRIGGER fail_second_mission_task
        BEFORE INSERT ON mission_tasks
        WHEN NEW.position = 2
        BEGIN
            SELECT RAISE(ABORT, 'injected task failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected task failure"):
        storage.create_mission(
            mission_id="mis_atomicfailure1",
            title="Must be atomic",
            goal="Persist all mission rows or none",
            tasks=["one", "two", "three"],
        )

    assert conn.in_transaction is False
    assert storage.get_mission("mis_atomicfailure1") is None
    storage.set_runtime_value("unrelated.write", {"ok": True})
    assert storage.get_mission("mis_atomicfailure1") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM mission_tasks WHERE mission_id = ?",
        ("mis_atomicfailure1",),
    ).fetchone()[0] == 0
    storage.close()


def test_storage_creates_consistent_database_backup(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    storage.add_memory(content="Backup me", namespace="runtime")

    backup = storage.backup_database()
    second_backup = storage.backup_database()

    backup_path = Path(backup["path"])
    assert backup["ok"] is True
    assert second_backup["path"] != backup["path"]
    assert backup_path.exists()
    assert backup_path.stat().st_size > 0
    clone = JarvisStorage(backup_path)
    clone.initialize()
    assert clone.counters()["memories"] == 1
    clone.close()
    storage.close()


def test_storage_lists_conversations_and_messages(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("History")
    user_id = storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="remember this",
    )
    assistant_id = storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="remembered",
    )

    conversations = storage.list_conversations()
    messages = storage.list_messages(conversation_id)

    assert conversations[0]["id"] == conversation_id
    assert conversations[0]["message_count"] == 2
    assert [message["id"] for message in messages] == [user_id, assistant_id]
    assert storage.get_conversation(conversation_id)["title"] == "History"
    storage.close()


def test_ensure_conversation_creates_row_then_add_message_succeeds(tmp_path):
    # Regression: a caller-supplied conversation_id that has no row must not make
    # add_message trip the messages.conversation_id foreign key (was a 500).
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    assert storage.get_conversation("conv_client_chosen") is None
    returned = storage.ensure_conversation("conv_client_chosen", "First turn")
    assert returned == "conv_client_chosen"
    row = storage.get_conversation("conv_client_chosen")
    assert row is not None and row["title"] == "First turn"

    # The FK insert that previously raised now works.
    message_id = storage.add_message(
        conversation_id="conv_client_chosen",
        role="user",
        content="hello",
    )
    assert storage.get_message(message_id)["conversation_id"] == "conv_client_chosen"

    # Idempotent: a second ensure with a different title neither duplicates nor renames.
    storage.ensure_conversation("conv_client_chosen", "Different title")
    conversations = [c for c in storage.list_conversations() if c["id"] == "conv_client_chosen"]
    assert len(conversations) == 1
    assert storage.get_conversation("conv_client_chosen")["title"] == "First turn"
    storage.close()


def test_storage_get_message_by_id_preserves_metadata(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Trace")
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="observable answer",
        metadata={"duration_ms": 123, "events": [{"type": "thought", "title": "route"}]},
    )

    message = storage.get_message(message_id)

    assert message is not None
    assert message["id"] == message_id
    assert message["conversation_id"] == conversation_id
    assert message["metadata"]["duration_ms"] == 123
    assert message["metadata"]["events"][0]["title"] == "route"
    assert storage.get_message("msg_missing") is None
    storage.close()


def test_storage_deletes_conversation_and_messages(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Temporary")
    storage.add_message(conversation_id=conversation_id, role="user", content="clear me")
    storage.add_message(conversation_id=conversation_id, role="assistant", content="cleared")

    deleted = storage.delete_conversation(conversation_id)

    assert deleted is True
    assert storage.get_conversation(conversation_id) is None
    assert storage.list_messages(conversation_id) == []
    assert storage.delete_conversation(conversation_id) is False
    storage.close()


def test_storage_updates_task_progress_and_searches_memory(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    mission = storage.create_mission(
        title="Memory mission",
        goal="Improve long-term memory",
        tasks=["Index memory", "Verify search"],
    )
    first_task = mission["tasks"][0]

    updated = storage.update_mission_task(first_task["id"], status="done", notes="Indexed")
    refreshed = storage.get_mission(mission["id"])
    memory = storage.add_memory(
        content="Jarvis memory uses SQLite FTS for local search.",
        namespace="runtime",
        tags=["memory", "fts"],
        importance=0.8,
    )
    hits = storage.search_memory("SQLite FTS", limit=5)

    assert updated is not None
    assert refreshed is not None
    assert refreshed["progress"] == 0.5
    assert memory["id"] in {item["id"] for item in hits}
    assert hits[0]["relevance"] > 0
    assert hits[0]["matched_terms"] == ["SQLite", "FTS"]
    assert "SQLite FTS" in hits[0]["snippet"]
    storage.close()


def test_storage_update_mission_task_requires_matching_mission_when_provided(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    first = storage.create_mission(
        title="First mission",
        goal="Keep task ownership isolated",
        tasks=["Owned task"],
    )
    second = storage.create_mission(
        title="Second mission",
        goal="Should not update first mission",
        tasks=["Other task"],
    )
    first_task = first["tasks"][0]

    rejected = storage.update_mission_task(
        first_task["id"],
        mission_id=second["id"],
        status="done",
        notes="wrong mission",
    )
    unchanged = storage.get_mission(first["id"])["tasks"][0]
    accepted = storage.update_mission_task(
        first_task["id"],
        mission_id=first["id"],
        status="done",
        notes="right mission",
    )

    assert rejected is None
    assert unchanged["status"] == "pending"
    assert unchanged["notes"] is None
    assert accepted is not None
    assert accepted["status"] == "done"
    assert accepted["notes"] == "right mission"
    storage.close()


def test_storage_merges_duplicate_memories_and_hybrid_search(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    first = storage.add_memory(
        content="LLM models live in D:\\jarvis\\models.",
        namespace="environment",
        tags=["models"],
        importance=0.5,
    )
    second = storage.add_memory(
        content="LLM models live in D:\\jarvis\\models.",
        namespace="environment",
        tags=["paths"],
        importance=0.8,
    )
    storage.add_memory(
        content="Operator prefers concise status updates.",
        namespace="preferences",
        tags=["operator"],
        importance=0.7,
    )

    hits = storage.search_memory("where are llm models stored jarvis", limit=5)

    assert first["id"] == second["id"]
    assert second["importance"] == 0.8
    assert {"models", "paths"}.issubset(set(second["tags"]))
    assert hits[0]["namespace"] == "environment"
    assert "D:\\jarvis\\models" in hits[0]["content"]
    storage.close()


def test_storage_mirrors_memory_to_obsidian_like_vault(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    memory = storage.add_memory(
        content="Jarvis should connect [[LLM runtime]] with [[GPU telemetry]] #runtime",
        namespace="architecture",
        tags=["jarvis", "graph"],
        importance=0.9,
    )
    graph = storage.memory_graph()

    note_path = storage.memory_vault.root / "architecture" / f"{memory['id']}.md"
    assert note_path.exists()
    note = note_path.read_text(encoding="utf-8")
    assert "namespace: \"architecture\"" in note
    assert "[[LLM runtime]]" in note
    assert graph["stats"]["notes"] == 1
    assert any(edge["target"] == "link:LLM runtime" for edge in graph["edges"])
    assert any(edge["target"] == "tag:runtime" for edge in graph["edges"])
    assert memory["id"] in graph["backlinks"]["LLM runtime"]
    storage.close()


def test_memory_graph_reads_from_db_not_disk_vault(tmp_path):
    # The read path builds the graph straight from the DB rows, so it neither depends on
    # nor rewrites the on-disk markdown vault on every request. Prove it by wiping the
    # mirrored notes after the write: the graph still carries the memory node (tags mined
    # from the body included) and the read does NOT re-mirror the files back to disk.
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    memory = storage.add_memory(
        content="Route [[LLM runtime]] to #gpu telemetry",
        namespace="architecture",
        tags=["jarvis"],
    )
    for path in list(storage.memory_vault.root.glob("**/*.md")):
        path.unlink()

    graph = storage.memory_graph()

    assert graph["stats"]["notes"] == 1
    assert any(node["id"] == memory["id"] for node in graph["nodes"])
    assert any(edge["target"] == "tag:gpu" for edge in graph["edges"])  # tag mined from body
    assert any(edge["target"] == "link:LLM runtime" for edge in graph["edges"])
    # A read must not resurrect the wiped notes on disk (no fsync-heavy per-request sync).
    assert not list(storage.memory_vault.root.glob("**/*.md"))
    storage.close()


def test_graph_from_memories_matches_disk_round_trip(tmp_path):
    # graph_from_memories (in-memory) must equal sync()+graph() (disk round-trip) exactly —
    # including tags mined from the note body — since that equivalence is what lets the read
    # path skip the fsync-heavy disk sync + cold re-read of every note.
    from jarvis_gpt.memory_vault import MemoryVault

    memories = [
        {
            "id": "m1", "namespace": "core", "content": "alpha [[beta]] #x", "tags": ["t1"],
            "importance": 0.5, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        {
            "id": "m2", "namespace": "ops", "content": "gamma names m1 and [[beta]] #y",
            "tags": [], "importance": 0.9, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-03T00:00:00Z",
        },
    ]
    in_mem = MemoryVault(tmp_path / "a").graph_from_memories(memories)
    disk = MemoryVault(tmp_path / "b")
    disk.sync(memories)
    on_disk = disk.graph()

    def shape(graph):
        nodes = {node["id"]: (node["kind"], node.get("label")) for node in graph["nodes"]}
        edges = sorted((e["source"], e["target"], e["kind"]) for e in graph["edges"])
        backlinks = {key: sorted(value) for key, value in graph["backlinks"].items()}
        return nodes, edges, backlinks, graph["stats"]

    assert shape(in_mem) == shape(on_disk)


def _make_file(storage, name, *, sha, source=None, mime="application/pdf"):
    return storage.create_file_record(
        name=name,
        stored_path=Path("/store") / name,
        sha256=sha,
        size=len(name),
        mime_type=mime,
        status="ready",
        source_path=source,
    )


def test_memory_graph_includes_document_nodes(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    f1 = _make_file(storage, "alpha.pdf", sha="a" * 64)
    f2 = _make_file(storage, "beta.xlsx", sha="b" * 64, mime="application/vnd.ms-excel")

    graph = storage.memory_graph()
    MemoryVaultResponse.model_validate(graph)  # extended payload still validates

    docs = [node for node in graph["nodes"] if node["kind"] == "document"]
    assert {node["id"] for node in docs} == {
        f"document:{f1['id']}",
        f"document:{f2['id']}",
    }
    alpha = next(node for node in docs if node["doc_id"] == f1["id"])
    assert alpha["mime"] == "application/pdf"
    assert alpha["size"] == len("alpha.pdf")
    assert alpha["status"] == "ready"
    assert graph["stats"]["documents"] == 2
    storage.close()


def test_memory_graph_links_memory_that_mentions_a_file(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    doc = _make_file(storage, "quarterly_report.pdf", sha="c" * 64)
    by_id = storage.add_memory(content=f"See file {doc['id']} for the numbers")
    by_name = storage.add_memory(content="Totals live in quarterly_report.pdf now")
    unrelated = storage.add_memory(content="quarterly numbers looked strong this cycle")

    graph = storage.memory_graph()
    mentions = {
        (edge["source"], edge["target"])
        for edge in graph["edges"]
        if edge["kind"] == "mentions"
    }
    assert (by_id["id"], f"document:{doc['id']}") in mentions
    assert (by_name["id"], f"document:{doc['id']}") in mentions
    # A memory that only shares a common word (not the full filename/id) is NOT linked.
    assert all(source != unrelated["id"] for source, _ in mentions)
    storage.close()


def test_memory_graph_filename_mentions_are_case_insensitive_and_exact(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    report = _make_file(storage, "report.pdf", sha="d" * 64)
    old_report = _make_file(storage, "old report.pdf", sha="e" * 64)
    report_copy = _make_file(storage, "report.pdf copy", sha="c" * 64)
    exact = storage.add_memory(content="Open REPORT.PDF for review")
    longer = storage.add_memory(content="Archive OLD REPORT.PDF after review")
    suffix = storage.add_memory(content="Move REPORT.PDF COPY into archive")

    graph = storage.memory_graph()
    mentions = {
        (edge["source"], edge["target"])
        for edge in graph["edges"]
        if edge["kind"] == "mentions"
    }

    assert (exact["id"], f"document:{report['id']}") in mentions
    assert (longer["id"], f"document:{old_report['id']}") in mentions
    assert (longer["id"], f"document:{report['id']}") not in mentions
    assert (suffix["id"], f"document:{report_copy['id']}") in mentions
    assert (suffix["id"], f"document:{report['id']}") not in mentions
    storage.close()


def test_memory_graph_caps_document_mentions(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_module, "_DOC_MENTION_EDGE_CAP", 1)
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    _make_file(storage, "first.pdf", sha="1" * 64)
    _make_file(storage, "second.pdf", sha="2" * 64)
    storage.add_memory(content="Compare first.pdf with second.pdf")

    graph = storage.memory_graph()
    mentions = [edge for edge in graph["edges"] if edge["kind"] == "mentions"]

    assert len(mentions) == 1
    storage.close()


def test_memory_graph_co_source_edges_skip_uploads(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    a = _make_file(storage, "a.pdf", sha="a" * 64, source=Path("C:/docs/a.pdf"))
    b = _make_file(storage, "b.pdf", sha="b" * 64, source=Path("C:/docs/b.pdf"))
    up = _make_file(storage, "up.pdf", sha="c" * 64, source=None)

    graph = storage.memory_graph()
    co_source = [edge for edge in graph["edges"] if edge["kind"] == "co-source"]
    pairs = {frozenset((edge["source"], edge["target"])) for edge in co_source}
    assert frozenset((f"document:{a['id']}", f"document:{b['id']}")) in pairs
    # An upload with no source_path is never grouped by folder.
    assert all(f"document:{up['id']}" not in pair for pair in pairs)
    storage.close()


def test_memory_graph_same_content_has_priority_over_co_source(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    shared_sha = "f" * 64
    a = _make_file(storage, "a.pdf", sha=shared_sha, source=Path("C:/docs/a.pdf"))
    b = _make_file(storage, "b.pdf", sha=shared_sha, source=Path("C:/docs/b.pdf"))

    graph = storage.memory_graph()
    pair = frozenset((f"document:{a['id']}", f"document:{b['id']}"))
    pair_edges = [
        edge
        for edge in graph["edges"]
        if frozenset((edge["source"], edge["target"])) == pair
    ]

    assert [edge["kind"] for edge in pair_edges] == ["same-content"]
    storage.close()


def test_memory_graph_large_same_day_uses_bucket_star_not_clique(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    count = 10  # > _DOC_DERIV_BUCKET_K (6): must collapse to a hub-and-star
    ids = [
        _make_file(storage, f"doc{i}.pdf", sha=f"{i:064d}", source=None)["id"]
        for i in range(count)
    ]

    graph = storage.memory_graph()
    hubs = [node for node in graph["nodes"] if node["kind"] == "daybucket"]
    assert len(hubs) == 1
    hub_id = hubs[0]["id"]

    co_day = [edge for edge in graph["edges"] if edge["kind"] == "co-day"]
    assert len(co_day) == count  # star: one edge per member, NOT count*(count-1)/2
    assert all(edge["target"] == hub_id for edge in co_day)
    assert {edge["source"] for edge in co_day} == {f"document:{fid}" for fid in ids}

    derived = [
        edge
        for edge in graph["edges"]
        if edge["kind"] in {"co-source", "co-day", "same-content"}
    ]
    assert len(derived) <= 3 * count  # global cap
    degree: dict[str, int] = {}
    for edge in derived:
        for endpoint in (edge["source"], edge["target"]):
            if endpoint.startswith("document:"):
                degree[endpoint] = degree.get(endpoint, 0) + 1
    assert all(value <= 8 for value in degree.values())  # per-doc cap
    storage.close()


def test_memory_graph_backward_compatible_without_documents(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    storage.add_memory(content="solo note [[other]] #tag", namespace="core")

    graph = storage.memory_graph()
    MemoryVaultResponse.model_validate(graph)
    assert graph["stats"]["documents"] == 0
    assert graph["stats"]["document_edges"] == 0
    assert not any(node["kind"] == "document" for node in graph["nodes"])
    degree_by_id = {node["id"]: node["degree"] for node in graph["nodes"]}
    memory_node = next(node for node in graph["nodes"] if node["kind"] == "memory")
    assert degree_by_id[memory_node["id"]] == 3
    assert degree_by_id["namespace:core"] == 1
    assert degree_by_id["link:other"] == 1
    assert degree_by_id["tag:tag"] == 1
    storage.close()


def test_memory_vault_rejects_dot_segment_namespace_escape(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    memory = storage.add_memory(content="Stay inside vault", namespace="..")

    escaped = storage.memory_vault.root.parent / f"{memory['id']}.md"
    safe = storage.memory_vault.root / "memory" / f"{memory['id']}.md"
    assert not escaped.exists()
    assert safe.exists()
    storage.rebuild_memory_vault()
    assert not escaped.exists()
    assert safe.exists()
    storage.close()


def test_storage_consolidates_existing_duplicate_memories(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    storage.add_memory(content="Use LAN launch mode by default.", namespace="instructions")
    with storage.connect() as conn:
        conn.execute(
            """
            INSERT INTO memories(
                id, namespace, content, tags, importance, created_at, updated_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mem_duplicate",
                "instructions",
                "Use LAN launch mode by default.",
                '["legacy"]',
                0.9,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                LEGACY_OWNER_USER_ID,
            ),
        )
        conn.commit()
    result = storage.consolidate_memories()
    hits = storage.search_memory("LAN launch mode", limit=10, namespaces=["instructions"])

    assert result["removed"] == 1
    assert len(hits) == 1
    assert "legacy" in hits[0]["tags"]
    note_path = storage.memory_vault.root / "instructions" / f"{hits[0]['id']}.md"
    assert note_path.exists()
    note = note_path.read_text(encoding="utf-8")
    assert 'tags: ["legacy"]' in note
    assert "importance: 0.9" in note
    assert len(list(storage.memory_vault.root.glob("**/*.md"))) == 1
    storage.close()


def test_memory_db_failure_rolls_back_without_writing_vault(monkeypatch, tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    def fail_fts(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated DB failure")

    monkeypatch.setattr(storage, "_replace_memory_fts", fail_fts)

    with pytest.raises(sqlite3.OperationalError, match="simulated DB failure"):
        storage.add_memory(content="must roll back")

    conn = storage.connect()
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert not list(storage.memory_vault.root.glob("**/*.md"))
    storage.close()


def test_memory_vault_failure_is_marked_then_repaired(monkeypatch, tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    original = storage.add_memory(
        content="Use local launch mode by default.",
        namespace="instructions",
    )
    with storage.connect() as conn:
        conn.execute(
            """
            INSERT INTO memories(
                id, namespace, content, tags, importance, created_at, updated_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mem_repair_keep",
                "instructions",
                "Use local launch mode by default.",
                '["legacy"]',
                0.9,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                LEGACY_OWNER_USER_ID,
            ),
        )
        conn.commit()
    storage.rebuild_memory_vault()
    original_upsert = storage.memory_vault.upsert_memory

    def fail_upsert(_memory):
        raise OSError("simulated mirror failure")

    monkeypatch.setattr(storage.memory_vault, "upsert_memory", fail_upsert)

    result = storage.consolidate_memories()

    assert result["removed"] == 1
    assert not storage.connect().in_transaction
    assert storage.get_runtime_value("memory_vault.repair_required")["required"] is True
    graph_ids = {node["id"] for node in storage.memory_graph()["nodes"]}
    assert original["id"] not in graph_ids
    assert "mem_repair_keep" in graph_ids

    monkeypatch.setattr(storage.memory_vault, "upsert_memory", original_upsert)
    storage.add_memory(content="Trigger pending mirror repair", namespace="repair")

    assert storage.get_runtime_value("memory_vault.repair_required") is None
    assert not (storage.memory_vault.root / "instructions" / f"{original['id']}.md").exists()
    assert (
        storage.memory_vault.root / "instructions" / "mem_repair_keep.md"
    ).exists()
    storage.close()


def test_storage_records_approval_gate(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    approval = storage.create_approval(
        title="Apply host patch",
        description="Needs operator review before changing host state.",
        requested_action="host.patch",
        risk="danger",
        payload={"path": "D:/jarvis"},
    )
    updated = storage.update_approval(
        approval["id"],
        status="approved",
        result={"operator": "test"},
    )
    audit = storage.list_audit(target_type="approval", target_id=approval["id"])

    assert updated is not None
    assert updated["status"] == "approved"
    assert storage.counters()["approvals"] == 1
    assert {entry["action"] for entry in audit} == {"approval.request", "approval.update"}
    storage.close()


def test_storage_persists_runtime_values(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    saved = storage.set_runtime_value("experience.preferences", {"operator_name": "Tony"})
    loaded = storage.get_runtime_value("experience.preferences", {})
    rows = storage.list_runtime_values("experience.")

    assert saved["key"] == "experience.preferences"
    assert loaded == {"operator_name": "Tony"}
    assert rows[0]["value"] == {"operator_name": "Tony"}
    assert storage.counters()["runtime_kv"] == 1
    storage.close()


def test_runtime_values_are_isolated_from_additional_owner(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    legacy_actor = ActorContext(
        user_id=LEGACY_OWNER_USER_ID,
        preset_key="admin",
        source="test",
    )
    additional_owner = ActorContext(
        user_id="usr_additional_owner",
        preset_key="owner",
        source="test",
    )

    with bind_actor(legacy_actor):
        legacy_saved = storage.set_runtime_value(
            "experience.preferences", {"private": "legacy"}
        )
    with bind_actor(additional_owner):
        assert storage.get_runtime_value("experience.preferences") is None
        owner_saved = storage.set_runtime_value(
            "experience.preferences", {"private": "additional-owner"}
        )
        assert storage.get_runtime_value("experience.preferences") == {
            "private": "additional-owner"
        }
        assert storage.counters()["runtime_kv"] == 1
        assert len(storage.list_runtime_values()) == 1
    with bind_actor(legacy_actor):
        assert storage.get_runtime_value("experience.preferences") == {
            "private": "legacy"
        }
        assert storage.counters()["runtime_kv"] == 1
        assert len(storage.list_runtime_values()) == 1

    assert legacy_saved["key"] == "experience.preferences"
    assert owner_saved["key"] == "user.usr_additional_owner.experience.preferences"
    storage.close()


def test_runtime_long_keys_are_collision_resistant_for_nonlegacy_actor(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    actor = ActorContext(
        user_id="usr_long_runtime",
        preset_key="owner",
        source="test",
    )
    common = f"runtime.long.{('x' * 1_100)}"
    first_key = f"{common}.first"
    second_key = f"{common}.second"

    with bind_actor(actor):
        first = storage.set_runtime_value(first_key, {"value": 1})
        second = storage.set_runtime_value(second_key, {"value": 2})
        assert first["key"] != second["key"]
        assert len(first["key"]) == 1_024
        assert len(second["key"]) == 1_024
        assert storage.get_runtime_value(first_key) == {"value": 1}
        assert storage.get_runtime_value(second_key) == {"value": 2}
        updated = storage.update_runtime_value_atomic(
            first_key,
            lambda value: {"value": value["value"] + 10},
        )
        assert updated == {"value": 11}
        assert storage.get_runtime_value(first_key) == {"value": 11}
        assert storage.delete_runtime_value(second_key) is True
        assert storage.get_runtime_value(second_key) is None
    storage.close()


def test_runtime_key_migrates_verified_legacy_160_char_checkpoint(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    actor = ActorContext(
        user_id=f"usr_{'a' * 72}",
        preset_key="owner",
        source="test",
    )
    request_hash = "b" * 64
    raw_key = f"agent.stream.interrupted.request.{request_hash}"
    scoped_key = f"user.{actor.user_id}.{raw_key}"
    legacy_key = scoped_key[:160]
    value = {
        "protocol": "jarvis.interrupted-stream.v2",
        "request_hash": request_hash,
        "answer": "partial",
        "terminal": False,
    }
    storage.connect().execute(
        "INSERT INTO runtime_kv(key, value, updated_at) VALUES (?, ?, ?)",
        (legacy_key, json.dumps(value), "2026-07-19T12:00:00+00:00"),
    )
    storage.connect().commit()

    with bind_actor(actor):
        assert storage.get_runtime_value(raw_key) == value
    keys = {
        row["key"]
        for row in storage.connect().execute("SELECT key FROM runtime_kv").fetchall()
    }
    assert scoped_key in keys
    assert legacy_key not in keys
    storage.close()


def test_latest_health_uses_one_newest_row_when_timestamps_tie(monkeypatch, tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    monkeypatch.setattr("jarvis_gpt.storage.utc_now", lambda: "2026-07-16T12:00:00+00:00")

    storage.record_health(component="llm", status="error", message="temporarily down")
    storage.record_health(component="llm", status="ok", message="recovered")

    rows = storage.latest_health(limit=20)
    assert len(rows) == 1
    assert rows[0]["component"] == "llm"
    assert rows[0]["status"] == "ok"
    assert rows[0]["message"] == "recovered"
    storage.close()


def test_complete_health_snapshot_is_atomic_when_one_component_insert_fails(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    good = [
        {
            "component": "runtime.home",
            "status": "ok",
            "message": "available",
        },
        {
            "component": "llm.router",
            "status": "ok",
            "message": "responding",
        },
    ]
    first_marker = storage.record_health_snapshot(good)
    conn = storage.connect()
    conn.execute(
        """
        CREATE TRIGGER fail_health_router_insert
        BEFORE INSERT ON health_snapshots
        WHEN NEW.component = 'llm.router' AND NEW.status = 'warn'
        BEGIN
            SELECT RAISE(ABORT, 'injected health failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected health failure"):
        storage.record_health_snapshot(
            [
                {**good[0], "message": "new local result"},
                {
                    "component": "llm.router",
                    "status": "warn",
                    "message": "unavailable",
                },
            ]
        )

    assert conn.in_transaction is False
    rows = storage.latest_complete_health(limit=20)
    assert {row["status"] for row in rows} == {"ok"}
    assert {row["id"] for row in rows} == set(first_marker["row_ids"])
    assert conn.execute(
        "SELECT COUNT(*) FROM health_snapshots"
    ).fetchone()[0] == len(good)
    storage.close()


def test_qwen_profile_product_contract():
    from jarvis_gpt.config import (
        PROFILES,
        detect_repeated_token_degeneration,
        profile_public_dict,
    )

    qwen = PROFILES["qwen36-vl"]
    assert qwen.certification == "experimental"
    assert qwen.research_only is True
    assert qwen.interactive_certified is False
    assert qwen.default_recommended is False
    assert qwen.menu_visible is True
    assert qwen.requires_experimental_opt_in is False
    assert qwen.readiness_deadline_sec > 0
    public = profile_public_dict(qwen)
    assert public["certification"] == "experimental"
    assert public["vision_capable"] is True
    assert "Qwen" in qwen.certification_reason
    assert detect_repeated_token_degeneration("4") is False
    assert detect_repeated_token_degeneration(" ".join(["token"] * 20)) is True
