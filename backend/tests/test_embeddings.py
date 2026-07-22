from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.embeddings import (
    EmbeddingBackend,
    lexical_vector,
    reciprocal_rank_fusion,
    semantic_similarity_order,
    sparse_cosine,
)
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage


def test_lexical_vector_matches_inflection_over_unrelated():
    query = lexical_vector("видеокарты греются в играх")
    related = lexical_vector("видеокарта перегревается под нагрузкой, чистить кулер")
    unrelated = lexical_vector("любимый рецепт борща с говядиной")

    assert sparse_cosine(query, related) > sparse_cosine(query, unrelated)
    assert sparse_cosine(query, related) > 0.0


def test_lexical_vector_supports_cjk_and_korean_without_spaces():
    cases = [
        ("人工智能新闻", "今天的人工智能新闻与模型更新", "家庭烹饪食谱"),
        ("인공지능 뉴스", "오늘의 인공지능뉴스와 모델 업데이트", "가정 요리법"),
        ("人工知能ニュース", "今日の人工知能ニュースとモデル更新", "家庭料理のレシピ"),
    ]

    for query_text, related_text, unrelated_text in cases:
        query = lexical_vector(query_text)
        related = lexical_vector(related_text)
        unrelated = lexical_vector(unrelated_text)
        assert query
        assert sparse_cosine(query, related) > sparse_cosine(query, unrelated)


def test_reciprocal_rank_fusion_rewards_agreement():
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["a", "c", "b"]])
    assert fused["a"] > fused["b"]
    assert fused["a"] > fused["c"]


def test_embedding_backend_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_EMBEDDINGS_ENABLED", raising=False)
    settings = load_settings()
    backend = EmbeddingBackend(settings)

    assert backend.remote_enabled is False
    assert asyncio.run(backend.embed(["hello"])) is None
    # Falls back to the local lexical vector order.
    order = asyncio.run(
        semantic_similarity_order(
            backend,
            "видеокарта греется",
            ["рецепт супа", "видеокарта горячая"],
        )
    )
    assert order[0] == 1


def test_semantic_order_uses_remote_dense_vectors_when_available():
    class FakeDenseBackend:
        # Toy 2-d embeddings: query aligns with the second document.
        _vectors = {
            "q": [1.0, 0.0],
            "d0": [0.0, 1.0],
            "d1": [0.9, 0.1],
        }

        async def embed(self, texts):
            keys = ["q", "d0", "d1"]
            return [self._vectors[key] for key in keys[: len(texts)]]

    order = asyncio.run(semantic_similarity_order(FakeDenseBackend(), "q", ["d0", "d1"]))
    assert order[0] == 1


def test_hybrid_files_reranks_chunks_by_semantic_closeness(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    stored = tmp_path / "gpu-notes.txt"
    stored.write_text("gpu notes", encoding="utf-8")
    record = storage.create_file_record(
        name="gpu-notes.txt",
        stored_path=stored,
        sha256="abc",
        size=stored.stat().st_size,
        mime_type="text/plain",
        status="indexed",
        chunk_count=2,
    )
    hot = "Видеокарта греется под нагрузкой в играх, чистить кулер и термопасту."
    unrelated = "Видеокарта используется для рендеринга видео и монтажа роликов."
    storage.add_file_chunks(record["id"], [unrelated, hot])
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    context = agent._prepare_context("почему видеокарта перегревается в играх?", None)
    context.file_hits = storage.list_file_chunks(record["id"], limit=10)
    asyncio.run(
        agent._augment_semantic_files(context, "почему видеокарта перегревается в играх?")
    )

    assert context.file_hits[0]["content"] == hot
    assert context.file_hits[0].get("retrieval") == "hybrid"
    storage.close()


def test_hybrid_files_falls_back_to_recent_chunks_without_lexical_overlap(monkeypatch, tmp_path):
    # Force a lexical miss so this test isolates the recent semantic fallback.
    # Trigram FTS intentionally matches inflected multilingual word forms now.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    stored = tmp_path / "gpu-notes.txt"
    stored.write_text("gpu notes", encoding="utf-8")
    record = storage.create_file_record(
        name="gpu-notes.txt",
        stored_path=stored,
        sha256="abc",
        size=stored.stat().st_size,
        mime_type="text/plain",
        status="indexed",
        chunk_count=2,
    )
    hot = "Видеокарта перегревается под нагрузкой, нужно чистить кулер и менять термопасту."
    soup = "Рецепт борща с говядиной, свёклой и сметаной."
    storage.add_file_chunks(record["id"], [soup, hot])
    monkeypatch.setattr(storage, "search_file_chunks", lambda *_args, **_kwargs: [])
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    query = "перегрев видеокарты после игр"
    context = agent._prepare_context(query, None)
    assert context.file_hits == []  # sanity: zero lexical overlap by construction
    asyncio.run(agent._augment_semantic_files(context, query))

    assert context.file_hits, "recent-chunk fallback should supply file context"
    assert context.file_hits[0]["content"] == hot
    assert all(item.get("retrieval") == "semantic-recent" for item in context.file_hits)
    assert all(item["content"] != soup for item in context.file_hits)
    storage.close()


def test_hybrid_memory_surfaces_paraphrase_missed_by_keywords(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.add_memory(
        content="Видеокарта оператора перегревается под нагрузкой, надо чистить кулер.",
        namespace="environment",
        importance=0.7,
    )
    storage.add_memory(
        content="Любимый рецепт борща с говядиной и сметаной.",
        namespace="core",
        importance=0.7,
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    context = agent._prepare_context("почему видеокарты греются в играх?", None)
    asyncio.run(agent._augment_semantic_memory(context, "почему видеокарты греются в играх?"))

    assert context.memory_hits, "hybrid retrieval should surface at least one memory"
    assert "идеокарт" in context.memory_hits[0]["content"]
    assert context.memory_hits[0].get("retrieval") == "hybrid"
    storage.close()
