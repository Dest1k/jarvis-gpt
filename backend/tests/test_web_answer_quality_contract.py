from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import jarvis_gpt.tools as tools_module
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def _dated_news_source(*, url: str, published: str, title: str) -> dict[str, object]:
    return {
        "rank": 1,
        "title": title,
        "url": url,
        "snippet": "Обстановка на направлении Комар — Богатырь, выводы и прогноз.",
        "excerpt": "Публичная сводка об обстановке на направлении Комар — Богатырь.",
        "published": published,
        "fetched": True,
        "tool": "web.fetch",
        "quality": "web-source",
    }


def test_bounded_news_analysis_synthesizes_only_after_date_filter(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    sources = [
        _dated_news_source(
            url="https://first.example/articles/20260717-front",
            published="2026-07-17T10:00:00+03:00",
            title="Обстановка на направлении Комар — Богатырь 17 июля",
        ),
        _dated_news_source(
            url="https://second.example/articles/20260718-front",
            published="2026-07-18T12:00:00+03:00",
            title="Обстановка на направлении Комар — Богатырь 18 июля",
        ),
        _dated_news_source(
            url="https://old.example/articles/20260716-front",
            published="2026-07-16T08:00:00+03:00",
            title="Обстановка на направлении Комар — Богатырь 16 июля",
        ),
    ]

    async def fake_research(_ctx, _args):
        return ToolRunResponse(
            tool="web.research",
            ok=True,
            summary="Research ok.",
            data={"sources": sources},
        )

    async def feed_must_not_run(*_args, **_kwargs):
        raise AssertionError("complete two-domain coverage must not require RSS fallback")

    async def fake_synthesis(_ctx, **kwargs):
        captured.update(kwargs)
        return {
            "attempted": True,
            "used": True,
            "reason": "grounded",
            "answer": (
                "Вывод основан на двух датированных сводках: "
                "https://first.example/articles/20260717-front"
            ),
        }

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setattr(tools_module, "_web_research", fake_research)
    monkeypatch.setattr(tools_module, "_web_answer_news_feed_sources", feed_must_not_run)
    monkeypatch.setattr(tools_module, "_web_answer_synthesis", fake_synthesis)

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
                        "Проанализируй обстановку на направлении Комар — Богатырь "
                        "за 17–18 июля 2026 года, дай выводы и прогноз"
                    ),
                    "date_from": "2026-07-17",
                    "date_to": "2026-07-18",
                    "vertical": "news",
                    "use_cache": False,
                },
            )
        )
    finally:
        storage.close()

    assert result.ok is True
    assert result.data["synthesis"]["used"] is True
    assert captured["date_window"] == (date(2026, 7, 17), date(2026, 7, 18))
    synthesized_urls = {source["url"] for source in captured["sources"]}
    assert synthesized_urls == {
        "https://first.example/articles/20260717-front",
        "https://second.example/articles/20260718-front",
    }
    assert "https://old.example/articles/20260716-front" not in synthesized_urls


class _StaticSynthesisLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def complete(self, _messages, **_kwargs):
        return SimpleNamespace(ok=True, content=self.answer)


def _run_synthesis(answer: str, *, date_window: tuple[date, date] | None = None):
    source = _dated_news_source(
        url="https://publisher.example/articles/20260718-front",
        published="2026-07-18T12:00:00+03:00",
        title="Сводка за 18 июля 2026 года",
    )
    ctx = SimpleNamespace(
        settings=SimpleNamespace(llm_enabled=True),
        llm=_StaticSynthesisLLM(answer),
    )
    return asyncio.run(
        tools_module._web_answer_synthesis(
            ctx,
            question="Проанализируй публичные сводки за 18 июля 2026 года",
            queries=["публичные сводки 18 июля 2026"],
            sources=[source],
            verification={"verdict": "supported", "confidence": 0.8},
            fallback_answer="Безопасный датированный дайджест.",
            date_window=date_window,
        )
    )


def test_bounded_news_synthesis_rejects_unsupported_old_current_year():
    result = _run_synthesis(
        (
            "Текущая обстановка относится к июлю 2024 года, поэтому сведения 2026 года "
            "следует считать ошибкой индексации. Источник: "
            "https://publisher.example/articles/20260718-front"
        ),
        date_window=(date(2026, 7, 18), date(2026, 7, 18)),
    )

    assert result["used"] is False
    assert result["reason"] == "rejected"
    assert result["rejection"] not in {"", "missing_source_url"}


def test_financial_market_query_is_fresh_day_data_and_not_shopping():
    question = "Что там по акциям на нефть?"
    normalized = question.casefold()

    assert tools_module._web_answer_looks_like_financial_market(normalized) is True
    assert tools_module._web_answer_infer_freshness(question) == "day"
    assert tools_module._web_answer_looks_like_shopping(normalized) is False


def test_synthesis_cleaner_preserves_paragraphs_and_markdown_lists():
    answer = """<think>hidden reasoning</think>
Первый абзац с фактом и источником https://publisher.example/one.

Второй абзац с пояснением https://publisher.example/two.

- Первый вывод https://publisher.example/one
- Второй вывод https://publisher.example/two
"""

    cleaned = tools_module._web_answer_clean_synthesis(answer)

    assert cleaned == (
        "Первый абзац с фактом и источником https://publisher.example/one.\n\n"
        "Второй абзац с пояснением https://publisher.example/two.\n\n"
        "- Первый вывод https://publisher.example/one\n"
        "- Второй вывод https://publisher.example/two"
    )


def test_synthesis_rejects_blanket_refusal_when_public_sources_exist():
    result = _run_synthesis(
        (
            "Я не могу предоставлять информацию об этой публичной обстановке и не имею "
            "доступа к актуальным данным, несмотря на приложенный источник: "
            "https://publisher.example/articles/20260718-front"
        ),
        date_window=(date(2026, 7, 18), date(2026, 7, 18)),
    )

    assert result["used"] is False
    assert result["reason"] == "rejected"
    assert result["rejection"] not in {"", "missing_source_url"}
