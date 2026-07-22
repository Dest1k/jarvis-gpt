from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import (
    WEB_ANSWER_CACHE_KEY,
    ToolRegistry,
    _multilingual_search_variants,
    _web_answer_cache_get,
    _web_answer_cache_key,
    _web_search_language_coverage,
)

LANGUAGES = ["ru", "en", "zh", "ko", "ja"]
REGIONS = {
    "ru": "ru-ru",
    "en": "en-us",
    "zh": "zh-cn",
    "ko": "ko-kr",
    "ja": "ja-jp",
}


def _coverage(*, complete: bool) -> dict:
    covered = list(LANGUAGES) if complete else ["ru"]
    matrix = {}
    for language in LANGUAGES:
        has_result = language in covered
        matrix[language] = {
            "status": "results" if has_result else "missing_translation",
            "translation": "translated" if has_result else "missing",
            "region": REGIONS[language],
            "scheduled": has_result,
            "result_count": 1 if has_result else 0,
        }
    return {
        "requested_languages": list(LANGUAGES),
        "complete": complete,
        "translation_complete": complete,
        "covered_languages": covered,
        "missing_languages": [item for item in LANGUAGES if item not in covered],
        "translated_languages": covered,
        "untranslated_languages": [item for item in LANGUAGES if item not in covered],
        "covered_count": len(covered),
        "requested_count": len(LANGUAGES),
        "languages": matrix,
    }


def test_translation_script_validation_rejects_same_russian_text_for_foreign_languages():
    class MalformedTranslationLLM:
        async def complete(self, *_args, **_kwargs):
            return SimpleNamespace(
                ok=True,
                content=json.dumps(
                    {language: "запуск проекта" for language in LANGUAGES},
                    ensure_ascii=False,
                ),
            )

    context = SimpleNamespace(
        settings=SimpleNamespace(llm_enabled=True),
        llm=MalformedTranslationLLM(),
    )

    variants = asyncio.run(_multilingual_search_variants(context, "запуск проекта", {}))

    assert [item["language"] for item in variants[1:]] == ["ru"]
    assert variants[0]["untranslated_languages"] == ["en", "zh", "ko", "ja"]


def test_language_neutral_identifier_can_be_searched_in_every_region():
    class IdentifierTranslationLLM:
        async def complete(self, *_args, **_kwargs):
            return SimpleNamespace(
                ok=True,
                content=json.dumps({language: "RTX 5090" for language in LANGUAGES}),
            )

    context = SimpleNamespace(
        settings=SimpleNamespace(llm_enabled=True),
        llm=IdentifierTranslationLLM(),
    )

    variants = asyncio.run(_multilingual_search_variants(context, "RTX 5090", {}))

    assert [item["language"] for item in variants[1:]] == LANGUAGES
    assert variants[0]["untranslated_languages"] == []


def test_language_coverage_matrix_distinguishes_results_from_missing_translation():
    variants = [
        {
            "language": "",
            "region": "wt-wt",
            "query": "запуск проекта",
            "translation_status": "original",
            "untranslated_languages": ["en", "zh", "ko", "ja"],
        },
        {
            "language": "ru",
            "region": "ru-ru",
            "query": "запуск проекта",
            "translation_status": "translated",
            "untranslated_languages": [],
        },
    ]

    coverage = _web_search_language_coverage(
        variants,
        LANGUAGES,
        scheduled_languages={"ru"},
        result_counts={"ru": 2},
    )

    assert coverage["complete"] is False
    assert coverage["covered_languages"] == ["ru"]
    assert coverage["missing_languages"] == ["en", "zh", "ko", "ja"]
    assert coverage["languages"]["ru"]["status"] == "results"
    assert coverage["languages"]["en"]["status"] == "missing_translation"


def test_web_search_exposes_complete_default_language_coverage(monkeypatch, tmp_path):
    class TranslationLLM:
        async def complete(self, *_args, **_kwargs):
            return SimpleNamespace(
                ok=True,
                content=json.dumps(
                    {
                        "ru": "надежный поиск",
                        "en": "reliable search",
                        "zh": "可靠搜索",
                        "ko": "신뢰할 수 있는 검색",
                        "ja": "信頼できる検索",
                    },
                    ensure_ascii=False,
                ),
            )

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}

        def __init__(self, region: str):
            self.text = (
                '<a class="result__a" href="https://'
                f'{region}.example/result">{region} result</a>'
                f'<a class="result__snippet">{region} evidence</a>'
            )
            self.content = self.text.encode()

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def get(self, url, *, headers):
            del headers
            region = parse_qs(urlsplit(url).query).get("kl", ["wt-wt"])[0]
            return FakeResponse(region)

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setattr("jarvis_gpt.tools.httpx.AsyncClient", FakeClient)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, TranslationLLM())
    try:
        result = asyncio.run(
            tools.run(
                "web.search",
                {
                    "query": "надежный поиск",
                    "provider": "duckduckgo",
                    "mode": "DEEP_RESEARCH",
                },
            )
        )
    finally:
        storage.close()

    assert result.ok is True
    assert result.data["complete"] is True
    assert result.data["language_coverage"]["requested_languages"] == LANGUAGES
    assert result.data["language_coverage"]["covered_languages"] == LANGUAGES
    assert result.data["language_coverage"]["missing_languages"] == []
    assert all(
        item["status"] == "results"
        for item in result.data["language_coverage"]["languages"].values()
    )


def test_web_answer_does_not_cache_partial_translation_then_retries_healthy(
    monkeypatch,
    tmp_path,
):
    calls: list[dict] = []

    async def fake_research(_ctx, args):
        calls.append(args)
        return ToolRunResponse(
            tool="web.research",
            ok=True,
            summary="Research returned useful evidence.",
            data={
                "sources": [
                    {
                        "rank": 1,
                        "title": "Widget reliability documentation",
                        "url": "https://docs.vendor.example/widget-reliability",
                        "snippet": "Widget reliability behavior and retry contract.",
                        "excerpt": "The Widget reliability contract requires retrying failures.",
                        "fetched": True,
                        "tool": "web.fetch",
                        "quality": "vendor-docs",
                        "evidence_id": "ev_widget",
                    }
                ],
                "language_coverage": _coverage(complete=len(calls) > 1),
            },
        )

    async def fake_verify(_ctx, _args):
        return ToolRunResponse(
            tool="web.verify",
            ok=True,
            summary="Verification verdict: supported.",
            data={"verification": {"verdict": "supported", "confidence": 0.82}},
        )

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools._web_research", fake_research)
    monkeypatch.setattr("jarvis_gpt.tools._web_verify", fake_verify)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    arguments = {
        "question": "Widget reliability retry contract",
        "mode": "FAST_FACT",
    }
    try:
        first = asyncio.run(tools.run("web.answer", arguments))
        second = asyncio.run(tools.run("web.answer", arguments))
        third = asyncio.run(tools.run("web.answer", arguments))
    finally:
        storage.close()

    assert calls[0]["languages"] == LANGUAGES
    assert first.ok is True
    assert first.data["complete"] is False
    assert first.data["cache"]["hit"] is False
    assert first.data["language_coverage"]["untranslated_languages"] == [
        "en",
        "zh",
        "ko",
        "ja",
    ]
    assert second.data["complete"] is True
    assert second.data["cache"]["hit"] is False
    assert third.data["cache"]["hit"] is True
    assert len(calls) == 2


def test_web_answer_cache_identity_includes_languages_and_explicit_translations():
    common = {
        "question": "Widget status",
        "explicit_query": "",
        "queries": ["Widget status"],
        "region": "wt-wt",
        "freshness": "",
        "vertical": "web",
        "max_sources": 4,
        "mode": "DEEP_RESEARCH",
    }

    all_languages = _web_answer_cache_key(
        **common,
        languages=LANGUAGES,
        translated_queries={"en": "Widget status"},
    )
    russian_only = _web_answer_cache_key(
        **common,
        languages=["ru"],
        translated_queries={"ru": "Статус Widget"},
    )
    changed_translation = _web_answer_cache_key(
        **common,
        languages=LANGUAGES,
        translated_queries={"en": "Widget current status"},
    )

    assert len({all_languages, russian_only, changed_translation}) == 3


def test_partial_language_coverage_cache_entry_is_never_replayed(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    cache_key = "partial-language-entry"
    inconsistent_coverage = _coverage(complete=False)
    inconsistent_coverage["complete"] = True
    storage.set_runtime_value(
        WEB_ANSWER_CACHE_KEY,
        [
            {
                "key": cache_key,
                "cached_at": "2026-07-22T00:00:00Z",
                "expires_at": time.time() + 60,
                "data": {
                    "answer": "Useful but only Russian evidence.",
                    "complete": True,
                    "language_coverage": inconsistent_coverage,
                },
            }
        ],
    )
    try:
        cached = _web_answer_cache_get(
            storage,
            cache_key,
            required_languages=LANGUAGES,
        )
    finally:
        storage.close()

    assert cached is None
