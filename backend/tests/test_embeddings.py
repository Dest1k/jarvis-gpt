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
