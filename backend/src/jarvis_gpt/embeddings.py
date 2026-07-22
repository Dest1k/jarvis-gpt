"""Hybrid semantic retrieval helpers.

Lexical search (BM25/LIKE) misses relevant memories when the operator phrases
things differently from how they were stored — different word forms, word order
or a paraphrase. No amount of chat-model scale fixes that, because retrieval is
a separate subsystem: the model can only use context it is actually given.

This module adds a semantic re-ranking signal that fuses with the existing
lexical ranking:

- ``lexical_vector`` builds a pure-Python fuzzy vector (word tokens + character
  trigrams). It needs no dependencies and no service, and already recovers
  morphology/word-order/typo matches that keyword search misses (important for
  Russian inflection). This is the always-on default.
- ``EmbeddingBackend`` optionally calls an OpenAI-compatible ``/embeddings``
  endpoint for true neural semantics when the operator configures one. When it
  is disabled or unreachable, retrieval degrades to the pure-Python vector, and
  then to plain lexical order — never worse than before.

The re-ranking runs over a bounded candidate pool per query, so the optional
remote backend is a single batched request and needs no persisted vectors.
"""

from __future__ import annotations

import math
import re
import unicodedata

import httpx

from .config import JarvisSettings

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def lexical_vector(text: str) -> dict[str, float]:
    """A sparse, L2-normalized fuzzy vector of a text (words + char trigrams)."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = _TOKEN_RE.findall(normalized)
    weights: dict[str, float] = {}
    for token in tokens:
        weights[f"w:{token}"] = weights.get(f"w:{token}", 0.0) + 1.0
        padded = f"#{token}#"
        for index in range(len(padded) - 2):
            key = f"t:{padded[index : index + 3]}"
            weights[key] = weights.get(key, 0.0) + 0.5
        # Chinese, Korean, and Japanese commonly omit ASCII-style word spaces.
        # Character bigrams keep short names and two-character queries searchable;
        # the normal trigrams still provide the more selective ranking signal.
        if any(ord(char) > 127 for char in token):
            for index in range(max(0, len(token) - 1)):
                key = f"b:{token[index : index + 2]}"
                weights[key] = weights.get(key, 0.0) + 0.35
    norm = math.sqrt(sum(value * value for value in weights.values()))
    if norm == 0.0:
        return {}
    return {key: value / norm for key, value in weights.items()}


def sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def dense_cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def reciprocal_rank_fusion(rankings: list[list[str]], *, k: int = 60) -> dict[str, float]:
    """Combine several rank orders into one score map (higher is better)."""

    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + position + 1)
    return scores


class EmbeddingBackend:
    """Optional OpenAI-compatible embeddings client with graceful degradation."""

    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    @property
    def remote_enabled(self) -> bool:
        return bool(
            self.settings.embeddings_enabled
            and self.settings.embeddings_base_url
            and self.settings.embeddings_model
        )

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.remote_enabled or not texts:
            return None
        payload = {"model": self.settings.embeddings_model, "input": texts}
        try:
            timeout = httpx.Timeout(min(self.settings.llm_timeout_sec, 60.0), connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.settings.embeddings_base_url}/embeddings",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except Exception:  # noqa: BLE001 - any failure degrades to the local vector
            return None
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            return None
        vectors: list[list[float]] = []
        for item in items:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list):
                return None
            vectors.append([float(value) for value in vector])
        return vectors


async def semantic_similarity_order(
    backend: EmbeddingBackend,
    query: str,
    documents: list[str],
) -> list[int]:
    """Return document indices ordered by similarity to the query (best first)."""

    if not documents:
        return []
    vectors = await backend.embed([query, *documents])
    if vectors is not None and len(vectors) == len(documents) + 1:
        query_vector = vectors[0]
        scores = [dense_cosine(query_vector, vectors[index + 1]) for index in range(len(documents))]
    else:
        query_vector_sparse = lexical_vector(query)
        scores = [sparse_cosine(query_vector_sparse, lexical_vector(doc)) for doc in documents]
    return sorted(range(len(documents)), key=lambda index: scores[index], reverse=True)
