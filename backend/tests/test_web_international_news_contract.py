from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import jarvis_gpt.tools as tools_module
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def test_relevant_search_languages_extend_five_language_baseline():
    assert tools_module._default_search_languages("reliable search") == [
        "ru",
        "en",
        "zh",
        "ko",
        "ja",
    ]
    assert tools_module._default_search_languages(
        "Новости Украины, России и Ирана"
    ) == ["ru", "en", "zh", "ko", "ja", "uk", "fa"]
    assert tools_module._default_search_languages(
        "Новини України та ایران"
    ) == ["ru", "en", "zh", "ko", "ja", "uk", "fa"]


def test_multi_country_news_queries_are_scoped_without_global_russia_suffix():
    question = "Доклад по основным новостям в Украине, России и Иране за сутки"

    queries = tools_module._web_answer_queries(
        question,
        freshness="day",
        vertical="news",
        date_window=(datetime(2026, 7, 22).date(), datetime(2026, 7, 23).date()),
    )

    assert len(queries) == 4
    assert any(query.startswith("Украина latest news") for query in queries)
    assert any(query.startswith("Россия latest news") for query in queries)
    assert any(query.startswith("Иран latest news") for query in queries)
    assert not any(f"{question} Россия новости" in query for query in queries)


def test_mandatory_topic_queries_precede_optional_variants_and_native_languages_match():
    question = "Доклад по новостям Украины, России и Ирана"
    queries = tools_module._web_answer_queries(
        question,
        explicit_query="последние события",
        variants=["optional one", "optional two"],
        vertical="news",
    )
    plans = tools_module._web_answer_balanced_query_languages(
        queries,
        ["ru", "en", "zh", "ko", "ja", "uk", "fa"],
        required_topics=["ukraine", "russia", "iran"],
    )

    assert queries == [
        "последние события",
        "Украина latest news international sources",
        "Россия latest news international sources",
        "Иран latest news international sources",
    ]
    assert "uk" in plans[1]
    assert "ru" in plans[2]
    assert "fa" in plans[3]
    assert sorted({language for plan in plans for language in plan}) == sorted(
        ["ru", "en", "zh", "ko", "ja", "uk", "fa"]
    )
    assert tools_module._web_answer_balanced_query_languages(
        queries,
        ["en"],
        required_topics=["ukraine", "russia", "iran"],
    ) == [["en"], ["en"], ["en"], ["en"]]


def test_report_markers_require_international_sources_but_narrow_lookup_does_not():
    report = tools_module._web_answer_global_news_policy(
        "Обзор основных новостей Ирана за сегодня"
    )
    narrow = tools_module._web_answer_global_news_policy(
        "Какая новость произошла сегодня в Иране?"
    )

    assert report == {
        "required": True,
        "min_non_ru_domains": 2,
        "required_topics": ["iran"],
        "unresolved_scopes": [],
    }
    assert narrow == {
        "required": False,
        "min_non_ru_domains": 0,
        "required_topics": [],
        "unresolved_scopes": [],
    }
    other_report = tools_module._web_answer_global_news_policy(
        "Доклад по главным новостям Китая, США и Израиля"
    )
    assert other_report == {
        "required": True,
        "min_non_ru_domains": 2,
        "required_topics": ["china", "united_states", "israel"],
        "unresolved_scopes": [],
    }
    india_report = tools_module._web_answer_global_news_policy(
        "Доклад по главным новостям Индии, Пакистана и ЕС"
    )
    assert india_report["required_topics"] == [
        "india",
        "pakistan",
        "european_union",
    ]
    unresolved = tools_module._web_answer_global_news_policy(
        "Доклад по новостям Бразилии, Аргентины и Чили"
    )
    assert unresolved["unresolved_scopes"] == [
        "бразилии",
        "аргентины",
        "чили",
    ]
    assert unresolved["required_topics"] == [
        "unresolved:бразилии",
        "unresolved:аргентины",
        "unresolved:чили",
    ]
    assert tools_module._web_answer_global_news_policy(
        "Доклад по основным новостям за сутки"
    )["unresolved_scopes"] == []


def test_topic_detection_does_not_match_country_substrings():
    assert tools_module._web_answer_requested_news_topics(
        "События в Тиране и история Пруссии"
    ) == []
    assert tools_module._web_answer_requested_news_topics(
        "Iran, Russia and Ukraine"
    ) == ["ukraine", "russia", "iran"]


def test_source_segment_does_not_trust_search_locale():
    foreign_locale_ru_domain = {
        "url": "https://example.ru/article",
        "search_language": "en",
        "search_languages": ["en"],
    }
    generic_without_document_language = {
        "url": "https://example.com/article",
        "search_language": "en",
        "search_languages": ["en"],
    }
    generic_verified_english = {
        **generic_without_document_language,
        "document_language": "en-US",
    }
    foreign_tld_verified_russian = {
        "url": "https://ren.tv/article",
        "document_language": "ru",
    }

    assert tools_module._web_answer_source_segment(foreign_locale_ru_domain)[0] == "ru"
    assert (
        tools_module._web_answer_source_segment(generic_without_document_language)[0]
        == "unknown"
    )
    assert tools_module._web_answer_source_segment(generic_verified_english)[0] == "non_ru"
    assert tools_module._web_answer_source_segment(foreign_tld_verified_russian)[0] == "ru"


def test_independent_source_coverage_uses_publisher_domains():
    sources = [
        {
            "url": "https://bbc.com/world/one",
            "source_segment": "non_ru",
        },
        {
            "url": "https://www.bbc.com/world/two",
            "source_segment": "non_ru",
        },
        {
            "url": "https://feeds.bbci.co.uk/news/three",
            "source_segment": "non_ru",
        },
        {
            "url": "https://news.bbci.co.uk/news/four",
            "source_segment": "non_ru",
        },
    ]

    coverage = tools_module._web_answer_international_source_coverage(
        sources,
        required=True,
        min_non_ru_domains=3,
    )

    assert coverage["non_ru_domains"] == ["bbc.com", "bbci.co.uk"]
    assert coverage["missing_non_ru_domains"] == 1
    assert coverage["complete"] is False


def test_international_coverage_counts_only_sources_relevant_to_requested_topics():
    sources = [
        {
            "url": "https://bbc.com/world/ukraine",
            "source_segment": "non_ru",
            "topic_matches": ["ukraine"],
        },
        {
            "url": "https://theguardian.com/world/iran",
            "source_segment": "non_ru",
            "topic_matches": ["iran"],
        },
        {
            "url": "https://aljazeera.com/news/wildfire",
            "source_segment": "non_ru",
            "topic_matches": [],
        },
    ]

    coverage = tools_module._web_answer_international_source_coverage(
        sources,
        required=True,
        min_non_ru_domains=2,
        required_topics=["ukraine", "russia", "iran"],
    )

    assert coverage["non_ru_domains"] == ["bbc.com", "theguardian.com"]
    assert coverage["complete"] is True


def test_rolling_24h_requires_timestamp_not_date_only():
    end = datetime(2026, 7, 23, 15, 30, tzinfo=tools_module.WEB_NEWS_TIMEZONE)
    start = end - timedelta(hours=24)
    date_only = {
        "title": "Украина: событие",
        "url": "https://example.com/2026/07/23/article",
        "published": "2026-07-23",
    }
    timestamped = {
        **date_only,
        "published": "2026-07-23T10:00:00+03:00",
    }
    naive_timestamp = {
        **date_only,
        "published": "2026-07-23T10:00:00",
    }
    trusted_naive_timestamp = {
        **naive_timestamp,
        "published_at": "2026-07-23T10:00:00",
        "segment_reason": "trusted_feed:https://publisher.example/rss",
        "publication_timezone": "Europe/Moscow",
    }

    assert (
        tools_module._web_answer_news_source_in_window(
            date_only,
            date_from=start.date(),
            date_to=end.date(),
            time_from=start,
            time_to=end,
        )
        is False
    )
    assert (
        tools_module._web_answer_news_source_in_window(
            timestamped,
            date_from=start.date(),
            date_to=end.date(),
            time_from=start,
            time_to=end,
        )
        is True
    )
    assert (
        tools_module._web_answer_news_source_in_window(
            naive_timestamp,
            date_from=start.date(),
            date_to=end.date(),
            time_from=start,
            time_to=end,
        )
        is False
    )
    assert (
        tools_module._web_answer_news_source_in_window(
            trusted_naive_timestamp,
            date_from=start.date(),
            date_to=end.date(),
            time_from=start,
            time_to=end,
        )
        is True
    )


def test_rolling_window_phrases_and_explicit_calendar_precedence():
    now = datetime(2026, 7, 23, 15, 30, tzinfo=tools_module.WEB_NEWS_TIMEZONE)
    for phrase in (
        "за последние сутки",
        "последние сутки",
        "past day",
        "24h",
    ):
        assert tools_module._web_answer_news_time_window({}, phrase, now=now) == (
            now - timedelta(hours=24),
            now,
        )
    assert (
        tools_module._web_answer_news_time_window(
            {"date_from": "2026-06-01", "date_to": "2026-06-02"},
            "доклад за сутки",
            now=now,
        )
        is None
    )


def test_fetched_article_timestamp_precedes_search_provider_timestamp():
    end = datetime(2026, 7, 23, 15, 30, tzinfo=tools_module.WEB_NEWS_TIMEZONE)
    start = end - timedelta(hours=24)
    conflicting = {
        "title": "Iran update",
        "url": "https://example.com/iran",
        "published": "2026-07-23T12:00:00+03:00",
        "extraction": {
            "article_dates": ["2026-07-20T12:00:00+03:00"],
            "schema_articles": [],
        },
    }

    assert (
        tools_module._web_answer_news_source_in_window(
            conflicting,
            date_from=start.date(),
            date_to=end.date(),
            time_from=start,
            time_to=end,
        )
        is False
    )


def test_multilingual_result_selection_keeps_late_native_hits_and_coverage_is_honest():
    raw = [
        {
            "url": f"https://ru.example/{index}",
            "search_language": "ru",
            "search_languages": ["ru"],
        }
        for index in range(5)
    ]
    raw.extend(
        [
            {
                "url": "https://ua.example/story",
                "search_language": "uk",
                "search_languages": ["uk"],
            },
            {
                "url": "https://ir.example/story",
                "search_language": "fa",
                "search_languages": ["fa"],
            },
        ]
    )

    selected = tools_module._balanced_multilingual_search_results(
        raw,
        ["ru", "uk", "fa"],
        limit=3,
    )
    assert [item["search_language"] for item in selected] == ["ru", "uk", "fa"]

    coverage = tools_module._web_search_language_coverage(
        [
            {"language": language, "region": region, "translation_status": "translated"}
            for language, region in (
                ("ru", "ru-ru"),
                ("uk", "uk-ua"),
                ("fa", "fa-ir"),
            )
        ],
        ["ru", "uk", "fa"],
        scheduled_languages={"ru", "uk", "fa"},
        result_counts={"ru": 1, "uk": 1},
    )
    assert coverage["complete"] is False
    assert coverage["missing_languages"] == ["fa"]


def test_translated_persian_query_can_establish_source_relevance():
    source = {
        "title": "اخبار ایران امروز",
        "url": "https://publisher.example/iran",
        "snippet": "گزارش تازه از ایران",
    }

    assert tools_module._web_answer_source_relevant(
        "آخرین اخبار ایران",
        source,
        preferred_domains=[],
        vertical="news",
    )


def test_research_failed_fetch_keeps_translated_query_provenance(monkeypatch, tmp_path):
    async def fake_search(_ctx, _args):
        return ToolRunResponse(
            tool="web.search",
            ok=True,
            summary="Search ok.",
            data={
                "results": [
                    {
                        "rank": 1,
                        "title": "Ukraine report",
                        "url": "https://example.com/ukraine",
                        "snippet": "Ukraine report",
                        "search_query": "Ukraine latest news",
                        "search_queries": ["Україна останні новини", "Ukraine latest news"],
                        "search_language": "uk",
                        "search_languages": ["uk", "en"],
                        "search_region": "uk-ua",
                        "search_regions": ["uk-ua", "en-us"],
                        "provider": "duckduckgo_html",
                        "providers": ["duckduckgo_html", "bing_html"],
                    }
                ]
            },
        )

    async def failed_fetch(*_args, **_kwargs):
        raise RuntimeError("blocked")

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(tools_module, "_web_search", fake_search)
    monkeypatch.setattr(tools_module, "_web_fetch", failed_fetch)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    registry = ToolRegistry(settings, storage, SimpleNamespace())
    try:
        result = asyncio.run(
            registry.run(
                "web.research",
                {
                    "query": "новости Украины",
                    "render_fallback": False,
                    "archive_fallback": False,
                    "use_cache": False,
                },
            )
        )
    finally:
        storage.close()

    assert result.ok is True
    source = result.data["sources"][0]
    assert source["fetched"] is False
    assert source["search_queries"] == [
        "Україна останні новини",
        "Ukraine latest news",
    ]
    assert source["search_languages"] == ["uk", "en"]
    assert source["search_regions"] == ["uk-ua", "en-us"]
    assert source["providers"] == ["duckduckgo_html", "bing_html"]


def test_render_fallback_retains_fetched_document_language(monkeypatch, tmp_path):
    async def fake_search(_ctx, _args):
        return ToolRunResponse(
            tool="web.search",
            ok=True,
            summary="Search ok.",
            data={
                "results": [
                    {
                        "rank": 1,
                        "title": "Iran update",
                        "url": "https://publisher.example/iran",
                        "snippet": "Iran update",
                        "search_language": "en",
                        "search_languages": ["en"],
                    }
                ]
            },
        )

    async def fake_fetch(_ctx, _args):
        return ToolRunResponse(
            tool="web.fetch",
            ok=True,
            summary="Fetched.",
            data={
                "url": "https://publisher.example/iran",
                "text": "short",
                "content_type": "text/html",
                "content_language": "en-US",
                "html_metadata": {"language": "en"},
                "evidence_id": None,
            },
        )

    async def fake_render(_ctx, _args):
        return ToolRunResponse(
            tool="web.render",
            ok=True,
            summary="Rendered.",
            data={
                "url": "https://publisher.example/iran",
                "text": "Iran update " * 100,
                "content_type": "text/plain",
                "evidence_id": None,
            },
        )

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(tools_module, "_web_search", fake_search)
    monkeypatch.setattr(tools_module, "_web_fetch", fake_fetch)
    monkeypatch.setattr(tools_module, "_web_render", fake_render)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    registry = ToolRegistry(settings, storage, SimpleNamespace())
    try:
        result = asyncio.run(
            registry.run(
                "web.research",
                {
                    "query": "Iran update",
                    "render_fallback": True,
                    "archive_fallback": False,
                    "use_cache": False,
                },
            )
        )
    finally:
        storage.close()

    assert result.ok is True
    assert result.data["sources"][0]["tool"] == "web.render"
    assert result.data["sources"][0]["document_language"] == "en"


def test_web_feed_reports_downloaded_bytes(monkeypatch, tmp_path):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<rss><channel><title>Publisher</title><item>"
        "<title>Iran update</title><link>https://publisher.example/iran</link>"
        "<pubDate>Thu, 23 Jul 2026 10:00:00 +0300</pubDate>"
        "</item></channel></rss>"
    )

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/rss+xml; charset=utf-8"}

        async def aiter_bytes(self):
            yield xml.encode("utf-8")

    class StreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        def stream(self, *_args, **_kwargs):
            return StreamContext()

    async def allow_url(url):
        return url

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tools_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(tools_module, "_validate_public_http_url_async", allow_url)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    try:
        result = asyncio.run(
            tools_module._web_feed(
                SimpleNamespace(storage=storage),
                {"url": "https://publisher.example/rss", "limit": 10},
            )
        )
    finally:
        storage.close()

    assert result.ok is True
    assert result.data["bytes_read"] == len(xml.encode("utf-8"))


def test_global_ru_only_news_report_fails_closed(monkeypatch, tmp_path):
    now = datetime.now(tools_module.WEB_NEWS_TIMEZONE)
    published = (now - timedelta(hours=2)).isoformat()
    sources = [
        {
            "rank": index,
            "title": title,
            "url": url,
            "snippet": title,
            "excerpt": title,
            "published": published,
            "document_language": "ru",
            "fetched": True,
            "tool": "web.fetch",
            "quality": "web-source",
        }
        for index, (title, url) in enumerate(
            (
                ("Главные новости Украины", "https://one.ru/ukraine/1"),
                ("Главные новости России", "https://two.ru/russia/1"),
                ("Главные новости Ирана", "https://three.ru/iran/1"),
            ),
            start=1,
        )
    ]

    async def fake_research(_ctx, _args):
        return ToolRunResponse(
            tool="web.research",
            ok=True,
            summary="RU-only research.",
            data={"sources": sources},
        )

    async def no_foreign_feed_sources(*_args, **_kwargs):
        return [], [{"tool": "web.feed", "ok": False, "summary": "offline"}]

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(tools_module, "_web_research", fake_research)
    monkeypatch.setattr(
        tools_module,
        "_web_answer_news_feed_sources",
        no_foreign_feed_sources,
    )
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    registry = ToolRegistry(settings, storage, SimpleNamespace())
    try:
        result = asyncio.run(
            registry.run(
                "web.answer",
                {
                    "question": (
                        "Доклад по основным новостям в Украине, России и Иране за сутки"
                    ),
                    "query": "последние события",
                    "vertical": "news",
                    "use_cache": True,
                },
            )
        )
        cache_records = storage.get_runtime_value(tools_module.WEB_ANSWER_CACHE_KEY, [])
    finally:
        storage.close()

    assert result.ok is False
    assert result.data["complete"] is False
    assert result.data["topic_coverage"] == {
        "required_topics": ["ukraine", "russia", "iran"],
        "covered_topics": ["ukraine", "russia", "iran"],
        "missing_topics": [],
        "complete": True,
    }
    coverage = result.data["international_source_coverage"]
    assert coverage["required"] is True
    assert coverage["min_non_ru_domains"] == 2
    assert coverage["non_ru_domains"] == []
    assert coverage["missing_non_ru_domains"] == 2
    assert coverage["complete"] is False
    assert result.data["news"]["window_kind"] == "rolling_24h"
    assert "RU-only" in result.data["answer"]
    assert cache_records == []


def test_global_news_report_completes_with_topics_and_two_non_ru_publishers(
    monkeypatch,
    tmp_path,
):
    now = datetime.now(tools_module.WEB_NEWS_TIMEZONE)
    published = (now - timedelta(hours=2)).isoformat()
    sources = [
        {
            "rank": 1,
            "title": "Ukraine latest news",
            "url": "https://bbc.co.uk/news/ukraine",
            "snippet": "Ukraine latest news",
            "excerpt": "Ukraine latest news",
            "published": published,
            "document_language": "en",
            "fetched": True,
            "tool": "web.fetch",
            "quality": "web-source",
        },
        {
            "rank": 2,
            "title": "Russia latest news",
            "url": "https://theguardian.com/world/russia",
            "snippet": "Russia latest news",
            "excerpt": "Russia latest news",
            "published": published,
            "document_language": "en",
            "fetched": True,
            "tool": "web.fetch",
            "quality": "web-source",
        },
        {
            "rank": 3,
            "title": "Iran latest news",
            "url": "https://aljazeera.com/news/iran",
            "snippet": "Iran latest news",
            "excerpt": "Iran latest news",
            "published": published,
            "document_language": "en",
            "fetched": True,
            "tool": "web.fetch",
            "quality": "web-source",
        },
    ]

    async def fake_research(_ctx, args):
        languages = list(args["languages"])
        matrix = {
            language: {
                "status": "results",
                "translation": "translated",
                "region": tools_module._SEARCH_LANGUAGE_REGIONS[language],
                "scheduled": True,
                "result_count": 1,
            }
            for language in languages
        }
        return ToolRunResponse(
            tool="web.research",
            ok=True,
            summary="International research.",
            data={
                "sources": sources,
                "language_coverage": {
                    "requested_languages": languages,
                    "complete": True,
                    "translation_complete": True,
                    "covered_languages": languages,
                    "missing_languages": [],
                    "translated_languages": languages,
                    "untranslated_languages": [],
                    "covered_count": len(languages),
                    "requested_count": len(languages),
                    "languages": matrix,
                },
            },
        )

    async def feed_must_not_run(*_args, **_kwargs):
        raise AssertionError("complete international search coverage needs no RSS fallback")

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(tools_module, "_web_research", fake_research)
    monkeypatch.setattr(
        tools_module,
        "_web_answer_news_feed_sources",
        feed_must_not_run,
    )
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    registry = ToolRegistry(settings, storage, SimpleNamespace())
    try:
        result = asyncio.run(
            registry.run(
                "web.answer",
                {
                    "question": (
                        "Доклад по основным новостям в Украине, России и Иране за сутки"
                    ),
                    "vertical": "news",
                    "use_cache": False,
                },
            )
        )
    finally:
        storage.close()

    assert result.ok is True
    assert result.data["complete"] is True
    assert result.data["international_source_coverage"]["complete"] is True
    assert result.data["international_source_coverage"]["non_ru_domains"] == [
        "aljazeera.com",
        "bbc.co.uk",
        "theguardian.com",
    ]
    assert result.data["topic_coverage"]["complete"] is True
    assert result.data["topic_coverage"]["covered_topics"] == [
        "ukraine",
        "russia",
        "iran",
    ]
    assert "### Украина" in result.data["answer"]
    assert "### Россия" in result.data["answer"]
    assert "### Иран" in result.data["answer"]


def test_deterministic_news_fallback_is_structured_by_requested_topic():
    sources = [
        {
            "title": "Ukraine update",
            "url": "https://bbc.co.uk/news/ukraine",
            "published": "2026-07-23T10:00:00+03:00",
            "published_date": "2026-07-23",
            "excerpt": "Ukraine update",
            "source_segment": "non_ru",
            "topic_matches": ["ukraine"],
        },
        {
            "title": "Россия: главное событие",
            "url": "https://example.ru/russia",
            "published": "2026-07-23T11:00:00+03:00",
            "published_date": "2026-07-23",
            "excerpt": "Россия: главное событие",
            "source_segment": "ru",
            "topic_matches": ["russia"],
        },
        {
            "title": "Iran update",
            "url": "https://theguardian.com/world/iran",
            "published": "2026-07-23T12:00:00+03:00",
            "published_date": "2026-07-23",
            "excerpt": "Iran update",
            "source_segment": "non_ru",
            "topic_matches": ["iran"],
        },
    ]

    answer = tools_module._format_web_news_answer(
        sources=sources,
        date_from=datetime(2026, 7, 23).date(),
        date_to=datetime(2026, 7, 23).date(),
        required_topics=["ukraine", "russia", "iran"],
    )

    assert "### Украина" in answer
    assert "### Россия" in answer
    assert "### Иран" in answer
    assert "(вне RU-сегмента)" in answer
    assert "(RU-сегмент)" in answer
    assert "https://bbc.co.uk/news/ukraine" in answer
    assert "https://example.ru/russia" in answer
    assert "https://theguardian.com/world/iran" in answer


def test_deterministic_news_fallback_keeps_relevant_non_ru_source_in_topic_limit():
    sources = [
        {
            "title": f"RU item {index}",
            "url": f"https://example.ru/russia-{index}",
            "published_date": "2026-07-23",
            "source_segment": "ru",
            "topic_matches": ["russia"],
        }
        for index in range(4)
    ]
    sources.append(
        {
            "title": "International Russia report",
            "url": "https://theguardian.com/world/russia",
            "published_date": "2026-07-23",
            "source_segment": "non_ru",
            "topic_matches": ["russia"],
        }
    )

    answer = tools_module._format_web_news_answer(
        sources=sources,
        date_from=datetime(2026, 7, 23).date(),
        date_to=datetime(2026, 7, 23).date(),
        required_topics=["russia"],
    )

    assert "https://theguardian.com/world/russia" in answer
    assert answer.count("https://") == 3
