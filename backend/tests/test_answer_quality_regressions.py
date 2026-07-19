from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import MethodType, SimpleNamespace

import pytest
from jarvis_gpt import agent as agent_module
from jarvis_gpt import tools as tools_module
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _freeze_financial_test_clock(monkeypatch):
    today = date(2026, 7, 19)
    now = datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: now)


def test_current_date_is_authoritative_and_last_week_uses_2026_window(monkeypatch):
    today = date(2026, 7, 19)
    monkeypatch.setattr(agent_module, "_moscow_today", lambda now=None: today)

    message = "Дай анализ обстановки на фронте за последнюю неделю"

    assert "current_date field is authoritative" in agent_module.WEB_SYNTHESIS_PROMPT
    assert agent_module._relative_date_window_for_message(message.casefold()) == (
        date(2026, 7, 13),
        today,
    )
    query = agent_module._web_research_query_from_message(message)
    assert query is not None
    assert "2026-07-13 2026-07-19" in query
    assert "2024" not in query


def test_public_battlefield_status_query_routes_to_web_research():
    message = "А что по обстановке на направлении Комар - Богатырь"

    assert agent_module._looks_like_current_public_events_query(message.casefold())
    assert agent_module._web_research_query_from_message(message) == message


def test_current_oil_and_stock_prices_route_to_web_not_shopping():
    messages = (
        "Какие цены на нефть сейчас?",
        "Что там по акциям на нефть?",
        "Какие цены сейчас? нефть Brent и WTI",
        "Какой сейчас курс акций нефтяных компаний?",
    )

    for message in messages:
        normalized = message.casefold()
        assert agent_module._looks_like_financial_market_query(normalized)
        assert agent_module._web_research_query_from_message(message) is not None
        assert tools_module._web_answer_looks_like_financial_market(normalized)
        assert not tools_module._web_answer_looks_like_shopping(normalized)
        assert tools_module._web_answer_infer_vertical(message) == "web"

    assert tools_module._web_answer_infer_freshness(messages[1]) == "day"


def test_short_oil_price_shorthand_still_requires_live_web():
    message = "Цены на нефть"

    assert agent_module._looks_like_financial_market_query(message.casefold())
    assert agent_module._web_research_query_from_message(message) == message


def test_named_shop_stock_quote_can_never_enter_catalog_search():
    message = "Какая цена акций Ozon сейчас?"
    plan = agent_module.TaskKernelPlan(
        route="web_research",
        mode="concise",
        intent="shopping_research",
        confidence=0.9,
        query=message,
    )

    assert agent_module._looks_like_financial_market_query(message.casefold())
    assert agent_module._deterministic_named_shop_keys(message, plan) == []


def test_short_gold_and_crypto_quotes_are_financial_live_web_requests():
    cases = {
        "А золото?": "commodity",
        "Цена биткоина сейчас?": "crypto",
    }

    for message, kind in cases.items():
        normalized = message.casefold()
        assert agent_module._looks_like_financial_market_query(normalized)
        assert agent_module._web_research_query_from_message(message) is not None
        assert tools_module._web_answer_looks_like_financial_market(normalized)
        assert tools_module._web_answer_financial_instrument_kind(message) == kind
        assert tools_module._web_answer_infer_freshness(message) == "day"


def test_financial_query_variants_do_not_mix_crude_quotes_with_company_shares():
    crude = tools_module._web_answer_financial_query_variant("Какие цены на нефть сейчас?")
    shares = tools_module._web_answer_financial_query_variant("Что по акциям нефтяных компаний?")

    assert "Brent WTI" in crude
    assert "USD per barrel" in crude
    assert "stock ticker" not in crude
    assert "stock ticker" in shares
    assert "share price" in shares


class _ContextRuntime:
    def __init__(self, messages: list[dict[str, str]]) -> None:
        self.messages = messages

    def _subject_from_recent_context(
        self,
        message: str,
        conversation_id: str | None,
    ) -> str | None:
        assert conversation_id == "conv-regression"
        return agent_module._pick_subject_from_messages(message, self.messages)


def test_bmw_spec_followup_keeps_the_recent_bmw_subject():
    messages = [
        {"role": "user", "content": "Что по BMW 3 серии на вторичном рынке?"},
        {"role": "assistant", "content": "Нашёл варианты BMW 3 серии."},
    ]
    followup = "Версия 1.5 АТ 2016 года, 136 лошадей, передний привод"
    runtime = _ContextRuntime(messages)

    query = agent_module.AgentRuntime._contextualize_web_query(
        runtime,
        followup,
        followup,
        "conv-regression",
    )

    assert "bmw" in query.casefold()
    assert "1.5" in query
    assert "2016" in query


def test_elliptical_price_followup_keeps_the_recent_oil_subject():
    messages = [
        {"role": "user", "content": "Цены на нефть Brent и WTI"},
        {"role": "assistant", "content": "Проверяю текущие котировки."},
    ]
    followup = "Какие цены сейчас?"
    runtime = _ContextRuntime(messages)

    query = agent_module.AgentRuntime._contextualize_web_query(
        runtime,
        followup,
        followup,
        "conv-regression",
    )

    assert "brent" in query.casefold()
    assert "wti" in query.casefold()


def test_raw_web_surfer_links_are_evidence_not_a_final_answer():
    raw_search = {
        "results": [
            {"title": "Brent quote", "url": "https://example.test/brent"},
            {"title": "WTI quote", "url": "https://example.test/wti"},
        ]
    }

    assert agent_module._web_surfer_answer_text(raw_search) == ""
    assert agent_module._web_surfer_answer_text({"sources": raw_search["results"]}) == ""
    assert agent_module._web_surfer_answer_text({"answer": "Краткий вывод"}) == "Краткий вывод"
    assert (
        agent_module._web_surfer_answer_text(
            {"answer": "Я не имею доступа к данным в реальном времени."}
        )
        == ""
    )


def test_conclusions_without_links_are_recognized_and_cleaned():
    request = "Краткие выводы напиши, а не ссылки на источники"
    answer = (
        "Brent вырос, WTI следует за ним: https://example.test/quote\n"
        "[Рыночная сводка](https://example.test/report) подтверждает динамику.\n\n"
        "Источники:\n"
        "1. https://example.test/source"
    )

    assert agent_module._web_research_followup_intent(request)
    assert agent_module._requests_conclusions_without_links(request)
    cleaned = agent_module._remove_source_links(answer)
    assert "Brent вырос" in cleaned
    assert "Рыночная сводка" in cleaned
    assert "http" not in cleaned
    assert "Источники" not in cleaned


def test_most_recent_concrete_subject_wins_over_an_older_latin_brand():
    messages = [
        {"role": "user", "content": "Что по BMW 3 серии?"},
        {"role": "assistant", "content": "Ответ про BMW."},
        {"role": "user", "content": "Цены на нефть Brent и WTI"},
        {"role": "assistant", "content": "Ответ про нефть."},
    ]

    subject = agent_module._pick_subject_from_messages("Какие цены сейчас?", messages)

    assert subject is not None
    assert "brent" in subject.casefold()
    assert "wti" in subject.casefold()
    assert "bmw" not in subject.casefold()


def test_contextual_financial_followup_cannot_be_downgraded_to_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("market context")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="Цены на нефть Brent и WTI",
    )
    storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="Смотрю котировки.",
    )
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="Какие цены сейчас?",
    )
    runtime = agent_module.AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    context = agent_module.AgentContext(
        conversation_id=conversation_id,
        memory_hits=[],
        file_hits=[],
        task_plan=agent_module.TaskKernelPlan(
            route="web_research",
            mode="concise",
            intent="current_market_data",
            confidence=0.8,
            query="Какие цены сейчас?",
        ),
    )
    captured: dict[str, str] = {}

    async def fake_intent(_self, _message, _context):
        return agent_module.IntentDecision(route="chat", confidence=0.95)

    async def fake_research(_self, message, query, **_kwargs):
        captured.update(message=message, query=query)
        return agent_module.DirectAction(answer="live web used", events=[])

    monkeypatch.setattr(runtime, "_understand_intent", MethodType(fake_intent, runtime))
    monkeypatch.setattr(runtime, "_run_web_research", MethodType(fake_research, runtime))
    try:
        action = asyncio.run(runtime._try_direct_action("Какие цены сейчас?", context))
    finally:
        storage.close()

    assert action is not None
    assert action.answer == "live web used"
    assert "brent" in captured["query"].casefold()
    assert "wti" in captured["query"].casefold()


def test_exact_false_realtime_refusal_is_rejected_in_all_synthesis_paths():
    refusal = (
        "Я не имею доступа к данным в реальном времени, поэтому проверьте котировки "
        "самостоятельно на портале https://market.example/quote"
    )
    source = {
        "title": "Brent market quote",
        "url": "https://market.example/quote",
        "excerpt": "Brent latest settlement 70.25 USD per barrel on 2026-07-17.",
    }

    assert not agent_module._valid_web_synthesis_answer(refusal)
    assert (
        tools_module._web_answer_synthesis_rejection(
            refusal,
            [source],
            question="Какие цены на нефть сейчас?",
        )
        == "capability_refusal"
    )


def test_financial_source_relevance_requires_instrument_anchors():
    question = "Какие цены на нефть сейчас?"
    phone = {
        "title": "Смартфоны: какие цены сейчас",
        "url": "https://shop.example/phones",
        "snippet": "Какие цены сейчас на популярные телефоны",
    }
    fx = {
        "title": "Курс доллара USD/RUB сейчас",
        "url": "https://fx.example/usdrub",
        "snippet": "Текущий валютный курс доллара к рублю",
    }
    brent = {
        "title": "Brent crude oil latest settlement",
        "url": "https://market.example/brent",
        "snippet": "Brent crude oil 70.25 USD per barrel, settlement 2026-07-17",
    }

    for irrelevant in (phone, fx):
        assert not tools_module._web_answer_source_relevant(
            question,
            irrelevant,
            preferred_domains=[],
            vertical="web",
        )
    assert tools_module._web_answer_source_relevant(
        question,
        brent,
        preferred_domains=[],
        vertical="web",
    )


def test_financial_identity_constraints_are_not_relevance_boosts():
    wti_question = "Какая сейчас цена WTI?"
    brent = {
        "title": "Brent crude oil latest settlement",
        "url": "https://market.example/brent",
        "excerpt": "Brent settlement 70.25 USD per barrel on 2026-07-17.",
    }
    apple = {
        "title": "Apple AAPL stock quote on Nasdaq",
        "url": "https://market.example/aapl",
        "excerpt": "AAPL share price 210.25 USD on 2026-07-17.",
    }

    assert not tools_module._web_answer_source_relevant(
        wti_question,
        brent,
        preferred_domains=[],
        vertical="web",
    )
    assert not tools_module._web_answer_source_relevant(
        "Какая цена акций Газпрома сейчас?",
        apple,
        preferred_domains=[],
        vertical="web",
    )
    assert tools_module._web_answer_financial_entity_terms(
        "Какая цена акций Газпрома сейчас?"
    ) == ["газпрома"]


def test_financial_answer_contract_rejects_unsupported_quote_and_accepts_grounded_one():
    source = {
        "title": "Brent crude oil latest settlement",
        "url": "https://market.example/brent",
        "excerpt": "Brent futures settlement 70.25 USD per barrel on 2026-07-17.",
    }
    grounded = (
        "Последняя доступная котировка Brent — фьючерсный settlement 70.25 USD за баррель "
        "на 2026-07-17; рынок закрыт. Источник: https://market.example/brent"
    )
    invented = grounded.replace("70.25", "99.99")

    assert (
        tools_module._web_answer_synthesis_rejection(
            grounded,
            [source],
            question="Какие цены на нефть сейчас?",
        )
        == ""
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            invented,
            [source],
            question="Какие цены на нефть сейчас?",
        )
        == "unsupported_financial_number"
    )


def test_financial_answer_rejects_wrong_identity_currency_and_stale_quote():
    current_source = {
        "title": "Brent crude oil latest settlement",
        "url": "https://market.example/brent",
        "excerpt": "Brent futures settlement 70.25 USD per barrel on 2026-07-17.",
    }
    wrong_currency = (
        "Последняя доступная котировка Brent — фьючерсный settlement 70.25 RUB за баррель "
        "на 2026-07-17; рынок закрыт. Источник: https://market.example/brent"
    )
    wti_answer = (
        "Последняя доступная котировка WTI — фьючерсный settlement 70.25 USD за баррель "
        "на 2026-07-17; рынок закрыт. Источник: https://market.example/brent"
    )
    stale_source = {
        **current_source,
        "excerpt": "Brent futures settlement 70.25 USD per barrel on 2024-07-18.",
    }
    stale_answer = (
        "Последняя доступная котировка Brent — фьючерсный settlement 70.25 USD за баррель "
        "на 2024-07-18; рынок закрыт. Источник: https://market.example/brent"
    )

    assert (
        tools_module._web_answer_synthesis_rejection(
            wrong_currency,
            [current_source],
            question="Какие цены на нефть сейчас?",
        )
        == "unsupported_financial_currency"
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            wti_answer,
            [current_source],
            question="Какая сейчас цена WTI?",
        )
        == "source_identity_mismatch"
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            stale_answer,
            [stale_source],
            question="Какие цены на нефть сейчас?",
        )
        == "stale_financial_quote"
    )


def test_fresh_page_date_cannot_relabel_an_old_quote_as_current():
    source = {
        "title": "Latest Brent market history update",
        "url": "https://market.example/brent-history",
        "published_date": "2026-07-18",
        "excerpt": "Historical Brent futures settlement was 74.00 USD per barrel on 2024-01-05.",
    }
    answer = (
        "Latest Brent futures settlement was 74.00 USD per barrel on 2026-07-17; "
        "this is the latest available quote. Source: https://market.example/brent-history"
    )

    assert (
        tools_module._web_answer_synthesis_rejection(
            answer,
            [source],
            question="What is the current Brent price?",
        )
        == "unsupported_financial_number"
    )


def test_multi_benchmark_values_cannot_be_swapped_between_sources():
    sources = [
        {
            "title": "Brent latest settlement",
            "url": "https://market.example/brent",
            "excerpt": "Brent futures settlement 70.25 USD per barrel on 2026-07-17.",
        },
        {
            "title": "WTI latest settlement",
            "url": "https://market.example/wti",
            "excerpt": "WTI futures settlement 65.12 USD per barrel on 2026-07-17.",
        },
    ]
    grounded = (
        "Brent futures settlement is 70.25 USD per barrel on 2026-07-17 "
        "(https://market.example/brent). WTI futures settlement is 65.12 USD per barrel "
        "on 2026-07-17 (https://market.example/wti). These are latest available quotes."
    )
    swapped = grounded.replace("70.25", "TMP").replace("65.12", "70.25").replace(
        "TMP", "65.12"
    )

    assert (
        tools_module._web_answer_synthesis_rejection(
            grounded,
            sources,
            question="Какие сейчас цены Brent и WTI?",
        )
        == ""
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            swapped,
            sources,
            question="Какие сейчас цены Brent и WTI?",
        )
        == "unsupported_financial_number"
    )


def test_unqualified_dollar_rate_cannot_turn_into_an_arbitrary_fx_pair():
    source = {
        "title": "EUR/USD exchange rate",
        "url": "https://fx.example/eurusd",
        "excerpt": "EUR/USD latest quote 1.08 USD per EUR on 2026-07-17.",
    }
    answer = (
        "The latest EUR/USD exchange rate is 1.08 USD per EUR on 2026-07-17. "
        "This is a current market quote. Source: https://fx.example/eurusd"
    )

    assert tools_module._web_answer_requested_currency_pair(
        "Какой сейчас курс доллара?"
    ) == ("USD", "RUB")
    assert (
        tools_module._web_answer_synthesis_rejection(
            answer,
            [source],
            question="Какой сейчас курс доллара?",
        )
        == "missing_currency_pair"
    )


def test_index_etf_and_bond_metrics_cannot_be_interchanged():
    spy = {
        "title": "SPDR S&P 500 ETF Trust (SPY)",
        "url": "https://market.example/spy",
        "excerpt": "SPY ETF market price was 680.25 USD on 2026-07-17.",
    }
    spy_as_index = (
        "The S&P 500 index level is 680.25 points on 2026-07-17, based on the SPY ETF. "
        "Source: https://market.example/spy"
    )
    bond_price = {
        "title": "OFZ 26238 bond market price",
        "url": "https://market.example/ofz26238",
        "excerpt": "OFZ 26238 bond market price was 57.25 RUB on MOEX on 2026-07-17.",
    }
    price_as_yield = (
        "OFZ 26238 bond current yield is 57.25 percent on MOEX on 2026-07-17. "
        "This is the latest quote. Source: https://market.example/ofz26238"
    )

    assert tools_module._web_answer_financial_instrument_kind(
        "Какой сейчас индекс S&P 500?"
    ) == "index"
    assert (
        tools_module._web_answer_synthesis_rejection(
            spy_as_index,
            [spy],
            question="Какой сейчас индекс S&P 500?",
        )
        in {
            "wrong_financial_instrument",
            "source_identity_mismatch",
            "source_metric_mismatch",
        }
    )
    assert tools_module._web_answer_financial_instrument_kind(
        "Какая сейчас доходность облигации ОФЗ 26238?"
    ) == "bond"
    assert (
        tools_module._web_answer_synthesis_rejection(
            price_as_yield,
            [bond_price],
            question="Какая сейчас доходность облигации ОФЗ 26238?",
        )
        == "source_metric_mismatch"
    )


def test_etf_nav_is_not_a_market_price_and_crypto_quote_cannot_be_week_old():
    nav_source = {
        "title": "USO ETF NAV",
        "url": "https://market.example/uso",
        "excerpt": "USO ETF NAV was 75.25 USD on NYSE on 2026-07-17.",
    }
    nav_as_price = (
        "USO ETF market price was 75.25 USD on NYSE on 2026-07-17. This is the latest "
        "available quote. Source: https://market.example/uso"
    )
    crypto_source = {
        "title": "Bitcoin BTC/USD spot quote",
        "url": "https://crypto.example/btcusd",
        "excerpt": "Bitcoin BTC/USD spot quote was 65000 USD on 2026-07-13.",
    }
    stale_crypto = (
        "Bitcoin BTC/USD spot quote was 65000 USD on 2026-07-13. This is the current "
        "exchange price. Source: https://crypto.example/btcusd"
    )

    assert tools_module._web_answer_financial_instrument_kind(
        "Какая сейчас рыночная цена ETF USO?"
    ) == "etf"
    assert (
        tools_module._web_answer_synthesis_rejection(
            nav_as_price,
            [nav_source],
            question="Какая сейчас рыночная цена ETF USO?",
        )
        == "source_metric_mismatch"
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            stale_crypto,
            [crypto_source],
            question="Какая сейчас цена биткоина?",
        )
        == "stale_financial_quote"
    )


def test_valid_trailing_zero_multi_equity_and_russian_fx_quotes_are_accepted():
    wti_source = {
        "title": "WTI latest settlement",
        "url": "https://market.example/wti",
        "excerpt": "WTI futures settlement was 65.10 USD per barrel on 2026-07-17.",
    }
    wti_answer = (
        "WTI futures settlement was 65.10 USD per barrel on 2026-07-17. This is the "
        "latest available market quote. Source: https://market.example/wti"
    )
    equity_sources = [
        {
            "title": "Apple AAPL stock quote",
            "url": "https://market.example/aapl",
            "excerpt": "Apple AAPL stock price was 210.25 USD on Nasdaq on 2026-07-17.",
        },
        {
            "title": "Microsoft MSFT stock quote",
            "url": "https://market.example/msft",
            "excerpt": "Microsoft MSFT stock price was 510.50 USD on Nasdaq on 2026-07-17.",
        },
    ]
    equity_answer = (
        "Apple AAPL stock price was 210.25 USD on Nasdaq on 2026-07-17 "
        "(https://market.example/aapl). Microsoft MSFT stock price was 510.50 USD on "
        "Nasdaq on 2026-07-17 (https://market.example/msft). These are latest quotes."
    )
    fx_source = {
        "title": "Курс доллара к рублю",
        "url": "https://fx.example/usdrub",
        "excerpt": "Один доллар США стоил 90.25 рубля на 2026-07-17.",
    }
    fx_answer = (
        "Последний курс доллара к рублю составил 90.25 рубля за доллар на 2026-07-17. "
        "Это подтверждённая котировка. Источник: https://fx.example/usdrub"
    )

    assert (
        tools_module._web_answer_synthesis_rejection(
            wti_answer,
            [wti_source],
            question="Какая сейчас цена WTI?",
        )
        == ""
    )
    assert all(
        tools_module._web_answer_source_relevant(
            "Какие сейчас цены акций Apple и Microsoft?",
            source,
            preferred_domains=[],
            vertical="web",
        )
        for source in equity_sources
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            equity_answer,
            equity_sources,
            question="Какие сейчас цены акций Apple и Microsoft?",
        )
        == ""
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            fx_answer,
            [fx_source],
            question="Какой сейчас курс доллара к рублю?",
        )
        == ""
    )


def test_fx_rate_requires_live_web_and_an_explicit_grounded_currency_pair():
    question = "Какой сейчас курс доллара к рублю?"
    normalized = question.casefold()
    source = {
        "title": "USD/RUB exchange rate",
        "url": "https://market.example/usdrub",
        "excerpt": "1 USD = 7.25 RUB, latest quote on 2026-07-17.",
    }
    grounded = (
        "Последняя доступная котировка валютной пары USD/RUB — 7.25 RUB за 1 USD "
        "на 2026-07-17. Это подтверждённое источником значение, а не оценка из памяти "
        "модели. Источник: https://market.example/usdrub"
    )

    assert agent_module._looks_like_financial_market_query(normalized)
    assert agent_module._web_research_query_from_message(question) is not None
    assert tools_module._web_answer_looks_like_financial_market(normalized)
    assert tools_module._web_answer_financial_instrument_kind(question) == "fx"
    assert tools_module._web_answer_infer_freshness(question) == "day"
    assert "currency pair" in tools_module._web_answer_financial_query_variant(question)
    assert (
        tools_module._web_answer_synthesis_rejection(
            grounded,
            [source],
            question=question,
        )
        == ""
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            grounded.replace("7.25", "6.50"),
            [source],
            question=question,
        )
        == "unsupported_financial_number"
    )
    assert (
        tools_module._web_answer_synthesis_rejection(
            grounded.replace("USD/RUB", "USD").replace("RUB за", "доллара за"),
            [source],
            question=question,
        )
        == "missing_currency_pair"
    )


def test_equity_source_relevance_does_not_accept_a_currency_exchange_page():
    question = "Какой сейчас курс акций Apple, а не доллара?"
    fx = {
        "title": "USD/EUR currency exchange rate",
        "url": "https://fx.example/usdeur",
        "snippet": "Current dollar and euro exchange rate",
    }
    equity = {
        "title": "Apple stock quote (AAPL) on Nasdaq",
        "url": "https://market.example/aapl",
        "snippet": "AAPL share price in USD, latest trade 2026-07-17",
    }

    assert not tools_module._web_answer_source_relevant(
        question,
        fx,
        preferred_domains=[],
        vertical="web",
    )
    assert tools_module._web_answer_source_relevant(
        question,
        equity,
        preferred_domains=[],
        vertical="web",
    )


def test_financial_identifiers_do_not_allow_class_contract_or_isin_collisions():
    cases = [
        ("What is the current BRK.B stock price?", "BRK.A", "BRK.B", "stock"),
        (
            "What is the current BRNQ26 futures contract price?",
            "BRNQ27",
            "BRNQ26",
            "futures contract",
        ),
        (
            "What is the current bond price for ISIN US0378331005?",
            "US0378331006",
            "US0378331005",
            "bond ISIN",
        ),
    ]
    for question, wrong_id, requested_id, descriptor in cases:
        source = {
            "title": "Current financial instrument quote",
            "url": "https://market.example/instrument",
            "excerpt": (
                f"{descriptor} {wrong_id} market price was 101.25 USD on NYSE OTC ICE "
                "at 2026-07-19 12:00 UTC."
            ),
        }
        answer = (
            f"{descriptor} {requested_id} market price was 101.25 USD on NYSE OTC ICE at "
            "2026-07-19 12:00 UTC. This is the latest available market quote for the "
            "requested instrument. Source: https://market.example/instrument"
        )
        assert tools_module._web_answer_synthesis_rejection(
            answer, [source], question=question
        ) == "source_identity_mismatch"


def test_exact_identifier_and_value_must_share_the_same_local_quote(monkeypatch):
    monkeypatch.setattr(
        tools_module,
        "_web_news_today",
        lambda now=None: date(2026, 7, 20),
    )
    cases = (
        (
            "What is the current BRK.B stock price?",
            "BRK.A stock price was 710000 USD on NYSE at 2026-07-17 20:00 UTC",
            "BRK.B stock price was 473.25 USD on NYSE at 2026-07-17 20:00 UTC",
            "BRK.B stock price was {value} USD on NYSE at 2026-07-17 20:00 UTC",
            "710000",
            "473.25",
        ),
        (
            "What is the current BRNQ26 futures contract price?",
            "BRNQ27 futures contract price was 90.00 USD per barrel on ICE at "
            "2026-07-17 20:00 UTC",
            "BRNQ26 futures contract price was 70.00 USD per barrel on ICE at "
            "2026-07-17 20:00 UTC",
            "BRNQ26 futures contract price was {value} USD per barrel on ICE at "
            "2026-07-17 20:00 UTC",
            "90.00",
            "70.00",
        ),
        (
            "What is the current bond price for ISIN US0378331005?",
            "Bond ISIN US0378331006 price was 101.25 USD OTC at "
            "2026-07-17 20:00 UTC",
            "Bond ISIN US0378331005 price was 99.75 USD OTC at "
            "2026-07-17 20:00 UTC",
            "Bond ISIN US0378331005 price was {value} USD OTC at "
            "2026-07-17 20:00 UTC",
            "101.25",
            "99.75",
        ),
    )
    separators = (". ", " and ", " while ", " whereas ", " и ", "; ", "\n")

    assert tools_module._web_answer_financial_exact_identifiers(
        "BRK.B BRNQ26 ISIN US0378331005"
    ) == {"BRK.B", "BRNQ26", "US0378331005"}

    for question, wrong_quote, requested_quote, answer_template, wrong, correct in cases:
        assert tools_module._web_answer_financial_entity_terms(question) == []
        for separator in separators:
            source = {
                "title": "Current financial instrument quotes",
                "url": "https://market.example/instruments",
                "excerpt": f"{wrong_quote}{separator}{requested_quote}.",
            }
            grounded = (
                answer_template.format(value=correct)
                + ". Source: https://market.example/instruments"
            )
            relabelled = (
                answer_template.format(value=wrong)
                + ". Source: https://market.example/instruments"
            )

            assert tools_module._web_answer_financial_synthesis_rejection(
                grounded,
                question=question,
                sources=[source],
            ) == ""
            assert tools_module._web_answer_financial_synthesis_rejection(
                relabelled,
                question=question,
                sources=[source],
            ) == "unsupported_financial_number"


def test_financial_citation_is_bound_to_its_quote_clause(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What are the current BRK.A and BRK.B stock prices?"
    sources = [
        {
            "title": "BRK.A live stock price",
            "url": "https://market.example/a",
            "excerpt": (
                "BRK.A stock price was 710000 USD on NYSE at "
                "2026-07-20 12:10 UTC."
            ),
        },
        {
            "title": "BRK.B live stock price",
            "url": "https://market.example/b",
            "excerpt": (
                "BRK.B stock price was 473.25 USD on NYSE at "
                "2026-07-20 12:20 UTC."
            ),
        },
    ]
    grounded = (
        "BRK.A stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC. "
        "Source: https://market.example/a. BRK.B stock price was 473.25 USD on "
        "NYSE at 2026-07-20 12:20 UTC. Source: https://market.example/b"
    )
    swapped = grounded.replace("https://market.example/a", "URL_TMP").replace(
        "https://market.example/b", "https://market.example/a"
    ).replace("URL_TMP", "https://market.example/b")

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=sources,
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        swapped,
        question=question,
        sources=sources,
    ) == "unsupported_financial_citation"

    shared_source = {
        "title": "Berkshire live stock prices",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "BRK.A stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC and "
            "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC."
        ),
    }
    shared_answer = (
        "BRK.A stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC and "
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "Source: https://market.example/berkshire"
    )
    assert tools_module._web_answer_financial_synthesis_rejection(
        shared_answer,
        question=question,
        sources=[shared_source],
    ) == ""


def test_repeated_equal_values_keep_identifier_and_timestamp_per_occurrence(
    monkeypatch,
):
    today = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: now)
    question = "What is the current BRK.B stock price?"

    for separator in (". ", " and ", " и ", "; ", "\n", ", "):
        source = {
            "title": "Current BRK.A and BRK.B stock prices",
            "url": "https://market.example/berkshire",
            "excerpt": (
                "BRK.A stock price was 65000 USD on NYSE at 2026-07-20 12:10 UTC"
                f"{separator}BRK.B stock price was 65000 USD on NYSE at "
                "2026-07-20 12:20 UTC."
            ),
        }
        grounded = (
            "BRK.B stock price was 65000 USD on NYSE at 2026-07-20 12:20 UTC. "
            "This is the current market quote. Source: https://market.example/berkshire"
        )

        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded,
            question=question,
            sources=[source],
        ) == "", repr(separator)
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded.replace("12:20 UTC", "12:10 UTC"),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_financial_metadata_numbers_are_not_quote_candidates(monkeypatch):
    today = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: now)
    question = "What is the current BRK.B stock price?"
    metadata_only = {
        "title": "Current BRK.B stock price page",
        "url": "https://market.example/brkb",
        "excerpt": (
            "BRK.B USD stock price page updated at 2026-07-20 12:20 UTC; "
            "the numeric quote is unavailable."
        ),
    }

    for injected_value in ("2026", "20", "12"):
        answer = (
            f"BRK.B stock price was {injected_value} USD on NYSE at "
            "2026-07-20 12:20 UTC. This is the current quote. "
            "Source: https://market.example/brkb"
        )
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question=question,
            sources=[metadata_only],
        ) == "unsupported_financial_number"

    real_2026_quote = {
        **metadata_only,
        "excerpt": (
            "BRK.B stock price was 2026 USD on NYSE at 2026-07-20 12:20 UTC."
        ),
    }
    real_2026_answer = (
        "BRK.B stock price was 2026 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/brkb"
    )
    assert tools_module._web_answer_financial_synthesis_rejection(
        real_2026_answer,
        question=question,
        sources=[real_2026_quote],
    ) == ""


def test_exact_identifiers_are_case_insensitive_and_single_letter_tickers_are_bound(
    monkeypatch,
):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)

    assert tools_module._web_answer_financial_exact_identifiers(
        "current brk.b price; brnq26 futures; isin us0378331005"
    ) == {"BRK.B", "BRNQ26", "US0378331005"}
    assert tools_module._web_answer_financial_exact_identifiers(
        "What is the current F stock price?"
    ) == {"F"}

    cases = (
        (
            "What is the current brk.b stock price?",
            "brk.a stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC and "
            "brk.b stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC.",
            "brk.b stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "473.25",
            "710000",
        ),
        (
            "What is the current F stock price?",
            "T stock price was 99 USD on NYSE at 2026-07-20 12:10 UTC and "
            "F stock price was 12.50 USD on NYSE at 2026-07-20 12:20 UTC.",
            "F stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "12.50",
            "99",
        ),
    )
    for question, excerpt, answer_template, correct, wrong in cases:
        source = {
            "title": "Current stock prices",
            "url": "https://market.example/stocks",
            "excerpt": excerpt,
        }
        grounded = (
            answer_template.format(value=correct)
            + "This is the current quote. Source: https://market.example/stocks"
        )
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded,
            question=question,
            sources=[source],
        ) == ""
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded.replace(correct, wrong, 1),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_value_first_comma_tuple_binds_the_nearest_following_identifier(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    source = {
        "title": "Current Berkshire stock prices",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "710000 USD, BRK.A stock price on NYSE at 2026-07-20 12:20 UTC, "
            "473.25 USD, BRK.B stock price on NYSE at 2026-07-20 12:20 UTC."
        ),
    }
    grounded = (
        "473.25 USD, BRK.B stock price on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=[source],
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded.replace("473.25", "710000", 1),
        question=question,
        sources=[source],
    ) == "unsupported_financial_number"
    first_tuple_answer = (
        "710000 USD, BRK.A stock price on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )
    assert tools_module._web_answer_financial_synthesis_rejection(
        first_tuple_answer,
        question="What is the current BRK.A stock price?",
        sources=[source],
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        first_tuple_answer.replace("710000", "473.25", 1),
        question="What is the current BRK.A stock price?",
        sources=[source],
    ) == "unsupported_financial_number"


def test_value_first_prose_tuples_do_not_inherit_the_previous_identifier(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    requested = (
        "473.25 USD is the NYSE stock price for BRK.B at 2026-07-20 12:20 UTC"
    )
    other = "710000 USD is the NYSE stock price for BRK.A at 2026-07-20 12:20 UTC"
    identifier_first_requested = (
        "BRK.B has an NYSE stock price of 473.25 USD at 2026-07-20 12:20 UTC"
    )
    identifier_first_other = (
        "BRK.A has an NYSE stock price of 710000 USD at 2026-07-20 12:20 UTC"
    )
    grounded = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )
    relabelled = grounded.replace("473.25", "710000", 1)

    quote_orders = (
        (requested, other),
        (other, requested),
        (identifier_first_requested, identifier_first_other),
        (identifier_first_other, identifier_first_requested),
    )
    for quote_order in quote_orders:
        source = {
            "title": "Current Berkshire stock prices",
            "url": "https://market.example/berkshire",
            "excerpt": f"{quote_order[0]}, {quote_order[1]}.",
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded,
            question=question,
            sources=[source],
        ) == ""
        assert tools_module._web_answer_financial_synthesis_rejection(
            relabelled,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_tuple_binding_ignores_identifier_headers_counts_and_mixed_direction(
    monkeypatch,
):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    grounded = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )
    relabelled = grounded.replace("473.25", "710000", 1)
    excerpts = (
        (
            "BRK.A/BRK.B quotes: 473.25 USD is the NYSE stock price for BRK.B at "
            "2026-07-20 12:20 UTC, "
            "710000 USD is the NYSE stock price for BRK.A at 2026-07-20 12:20 UTC."
        ),
        (
            "2 current quotes: BRK.A has an NYSE stock price of 710000 USD at "
            "2026-07-20 12:20 UTC, "
            "BRK.B has an NYSE stock price of 473.25 USD at 2026-07-20 12:20 UTC."
        ),
        (
            "BRK.A has an NYSE stock price of 710000 USD at 2026-07-20 12:20 UTC, "
            "473.25 USD is the "
            "NYSE stock price for BRK.B at 2026-07-20 12:20 UTC."
        ),
    )

    for excerpt in excerpts:
        source = {
            "title": "Current Berkshire stock prices",
            "url": "https://market.example/berkshire",
            "excerpt": excerpt,
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded,
            question=question,
            sources=[source],
        ) == ""
        assert tools_module._web_answer_financial_synthesis_rejection(
            relabelled,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_auxiliary_numbers_do_not_split_an_identifier_price_tuple(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    grounded = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )
    relabelled = grounded.replace("473.25", "710000", 1)
    other_quotes = (
        "BRK.A class 1 stock price was 710000 USD on NYSE",
        "BRK.A 1-day change, stock price was 710000 USD on NYSE",
        "BRK.A bid 709500 USD, stock price was 710000 USD on NYSE",
    )

    for other_quote in other_quotes:
        source = {
            "title": "Current Berkshire stock prices",
            "url": "https://market.example/berkshire",
            "excerpt": (
                f"{other_quote}, BRK.B stock price was 473.25 USD on NYSE at "
                "2026-07-20 12:20 UTC."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded,
            question=question,
            sources=[source],
        ) == ""
        assert tools_module._web_answer_financial_synthesis_rejection(
            relabelled,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_equity_price_cannot_be_grounded_by_volume_high_or_ask_occurrence(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    grounded = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )
    relabelled = grounded.replace("473.25", "710000", 1)
    role_quotes = (
        "BRK.B trading volume was 710000 shares",
        "BRK.B 52-week high was 710000 USD",
        "BRK.B ask was 710000 USD",
    )

    for role_quote in role_quotes:
        source = {
            "title": "Current BRK.B stock price and market data",
            "url": "https://market.example/brkb",
            "excerpt": (
                "BRK.B stock price was 473.25 USD on NYSE at "
                f"2026-07-20 12:20 UTC, {role_quote} on NYSE at "
                "2026-07-20 12:20 UTC."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded.replace("berkshire", "brkb"),
            question=question,
            sources=[source],
        ) == ""
        assert tools_module._web_answer_financial_synthesis_rejection(
            relabelled.replace("berkshire", "brkb"),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"

    exact_bypass_source = {
        "title": "Current BRK.B stock price and market data",
        "url": "https://market.example/brkb",
        "excerpt": (
            "BRK.B stock price was 473.25 USD, trading volume was 710000 shares "
            "on NYSE at 2026-07-20 12:20 UTC."
        ),
    }
    assert tools_module._web_answer_financial_synthesis_rejection(
        relabelled.replace("berkshire", "brkb"),
        question=question,
        sources=[exact_bypass_source],
    ) == "unsupported_financial_number"


def test_lowercase_plain_tickers_are_exact_only_in_financial_context(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)

    assert tools_module._web_answer_financial_exact_identifiers(
        "current aapl stock price"
    ) == {"AAPL"}
    assert tools_module._web_answer_financial_exact_identifiers(
        "current f stock price"
    ) == {"F"}
    for question in (
        "current stock price for aapl",
        "current stock aapl price",
        "what is the stock price of aapl",
        "price of aapl",
        "Aapl stock price",
    ):
        assert tools_module._web_answer_financial_exact_identifiers(question) == {"AAPL"}
    assert tools_module._web_answer_financial_exact_identifiers(
        "current msft quote"
    ) == {"MSFT"}
    assert tools_module._web_answer_financial_exact_identifiers(
        "current stock price for f"
    ) == {"F"}
    for prose in (
        "This stock price page has current market data",
        "Find current stock price and share quote",
        "and stock price was 10",
        "class stock price",
    ):
        assert tools_module._web_answer_financial_exact_identifiers(prose) == set()

    cases = (
        (
            "current aapl stock price",
            "Current NYSE USD stock price quotes: aaplx=710000 and "
            "aapl=473.25 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "aapl stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "473.25",
            "710000",
        ),
        (
            "current f stock price",
            "Current NYSE USD stock price quotes: t=99 and "
            "f=12.50 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "f stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "12.50",
            "99",
        ),
        (
            "current stock price for aapl",
            "Current NYSE USD stock price quotes: aaplx=710000 and "
            "aapl=473.25 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "aapl stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "473.25",
            "710000",
        ),
        (
            "current stock aapl price",
            "Current NYSE USD stock price quotes: aaplx=710000 and "
            "aapl=473.25 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "aapl stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "473.25",
            "710000",
        ),
        (
            "current stock price for f",
            "Current NYSE USD stock price quotes: t=99 and "
            "f=12.50 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "f stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "12.50",
            "99",
        ),
        (
            "current msft quote",
            "Current NYSE USD stock price quotes: msftx=710000 and "
            "msft=473.25 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "msft stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "473.25",
            "710000",
        ),
        (
            "price of aapl",
            "Current NYSE USD stock price quotes: aaplx=710000 and "
            "aapl=473.25 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "aapl stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "473.25",
            "710000",
        ),
        (
            "Aapl stock price",
            "Current NYSE USD stock price quotes: aaplx=710000 and "
            "aapl=473.25 USD stock price on NYSE at 2026-07-20 12:20 UTC.",
            "aapl stock price was {value} USD on NYSE at 2026-07-20 12:20 UTC. ",
            "473.25",
            "710000",
        ),
    )
    for question, excerpt, answer_template, correct, wrong in cases:
        source = {
            "title": "Current stock price quotes on NYSE",
            "url": "https://market.example/stocks",
            "excerpt": excerpt,
        }
        grounded = (
            answer_template.format(value=correct)
            + "This is the current quote. Source: https://market.example/stocks"
        )
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded,
            question=question,
            sources=[source],
        ) == ""
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded.replace(correct, wrong, 1),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_equal_quote_timestamps_ignore_page_update_metadata(monkeypatch):
    today = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: now)
    question = "What is the current BRK.B stock price?"
    source = {
        "title": "Current BRK.A and BRK.B stock prices",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "Page updated at 2026-07-20 12:00 UTC, BRK.A stock price was "
            "65000 USD on NYSE at 2026-07-20 12:10 UTC, BRK.B stock price was "
            "65000 USD on NYSE at 2026-07-20 12:20 UTC."
        ),
    }
    grounded = (
        "BRK.B stock price was 65000 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=[source],
    ) == ""
    for borrowed_time in ("12:10 UTC", "12:00 UTC"):
        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded.replace("12:20 UTC", borrowed_time, 1),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_publication_and_other_instrument_time_cannot_ground_quote_timestamp(
    monkeypatch,
):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    grounded = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )
    publication_only = {
        "title": "Current BRK.B stock price",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "Page updated at 2026-07-20 12:00 UTC, BRK.B stock price was "
            "473.25 USD on NYSE."
        ),
    }
    other_instrument_only = {
        "title": "Current BRK.A and BRK.B stock prices",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "BRK.A stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC, "
            "BRK.B stock price was 473.25 USD on NYSE."
        ),
    }

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded.replace("12:20 UTC", "12:00 UTC", 1),
        question=question,
        sources=[publication_only],
    ) == "unsupported_financial_number"
    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded.replace("12:20 UTC", "12:10 UTC", 1),
        question=question,
        sources=[other_instrument_only],
    ) == "unsupported_financial_number"

    direct_quote = {
        **publication_only,
        "excerpt": (
            "Page updated at 2026-07-20 12:00 UTC, BRK.B stock price was "
            "473.25 USD on NYSE at 2026-07-20 12:20 UTC."
        ),
    }
    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=[direct_quote],
    ) == ""


def test_count_header_is_not_a_quote_value(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    source = {
        "title": "Current BRK.A and BRK.B stock prices",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "Page updated at 2026-07-20 12:00 UTC, BRK.B: 2 current quotes, "
            "BRK.A stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC, "
            "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC."
        ),
    }
    grounded = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=[source],
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded.replace("473.25", "2", 1),
        question=question,
        sources=[source],
    ) == "unsupported_financial_number"


def test_explicit_quote_timestamp_scope_can_govern_multiple_tuples(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    source = {
        "title": "Current BRK.A and BRK.B stock prices",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "Stock prices as of 2026-07-20 12:20 UTC: BRK.A stock price was "
            "710000 USD on NYSE, BRK.B stock price was 473.25 USD on NYSE."
        ),
    }
    grounded = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:20 UTC. "
        "This is the current quote. Source: https://market.example/berkshire"
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=[source],
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded.replace("473.25", "710000", 1),
        question=question,
        sources=[source],
    ) == "unsupported_financial_number"


def test_quote_timestamp_scope_is_hard_bounded_and_subject_bound(monkeypatch):
    today = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: now)
    question = "What is the current BRK.B stock price?"
    answer = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:10 UTC. "
        "Source: https://market.example/berkshire"
    )
    leaking_excerpts = (
        "BRK.A prices as of 2026-07-20 12:10 UTC: BRK.A stock price was "
        "710000 USD on NYSE. BRK.B stock price was 473.25 USD on NYSE.",
        "BRK.A prices as of 2026-07-20 12:10 UTC: BRK.A stock price was "
        "710000 USD on NYSE; BRK.B stock price was 473.25 USD on NYSE.",
        "BRK.A prices as of 2026-07-20 12:10 UTC: BRK.A stock price was "
        "710000 USD on NYSE, BRK.B stock price was 473.25 USD on NYSE.",
    )
    for excerpt in leaking_excerpts:
        source = {
            "title": "BRK.A and BRK.B stock prices",
            "url": "https://market.example/berkshire",
            "excerpt": excerpt,
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"

    generic_scope = {
        "title": "BRK.A and BRK.B stock prices",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "Stock prices as of 2026-07-20 12:10 UTC: BRK.A stock price was "
            "710000 USD on NYSE, BRK.B stock price was 473.25 USD on NYSE."
        ),
    }
    assert tools_module._web_answer_financial_synthesis_rejection(
        answer,
        question=question,
        sources=[generic_scope],
    ) == ""


def test_current_price_rejects_non_live_quote_statuses(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    answer = (
        "BRK.B stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC. "
        "Source: https://market.example/berkshire"
    )
    statuses = (
        "predicted",
        "consensus target",
        "indicative",
        "pre-market",
        "delayed",
        "historical",
        "average",
        "adjusted",
        "fair",
        "estimated",
        "post-market",
        "previous close",
        "prior close",
        "yesterday closing",
        "theoretical",
        "reference",
        "unofficial",
        "simulated",
        "intraday high",
        "Hypothetical",
        "hypothetical",
        "Synthetic",
        "synthetic",
        "Fictional",
        "fictional",
    )
    for status in statuses:
        source = {
            "title": "BRK.B stock market data",
            "url": "https://market.example/berkshire",
            "excerpt": (
                f"BRK.B {status} stock price was 710000 USD on NYSE at "
                "2026-07-20 12:10 UTC."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number", status

    qualifier_layouts = (
        "Hypothetical: BRK.B stock price",
        "hypothetical: BRK.B stock price",
        "synthetic — BRK.B stock price",
        "BRK.B: Hypothetical stock price",
        "For BRK.B, Hypothetical stock price",
        "BRK.B (Hypothetical stock price)",
    )
    for qualified_role in qualifier_layouts:
        source = {
            "title": "BRK.B stock market data",
            "url": "https://market.example/berkshire",
            "excerpt": (
                f"{qualified_role} was 710000 USD on NYSE at "
                "2026-07-20 12:10 UTC."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number", qualified_role

    suffix_qualifiers = (
        "(hypothetical)",
        ", hypothetical only",
        ", not actual",
        ", false value",
    )
    for qualifier in suffix_qualifiers:
        source = {
            "title": "BRK.B stock market data",
            "url": "https://market.example/berkshire",
            "excerpt": (
                "BRK.B stock price was 710000 USD on NYSE at "
                f"2026-07-20 12:10 UTC {qualifier}."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number", qualifier

    live_source = {
        "title": "BRK.B live stock price",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "BRK.B stock price was 473.25 USD on NYSE at "
            "2026-07-20 12:10 UTC."
        ),
    }
    assert tools_module._web_answer_financial_synthesis_rejection(
        answer.replace("710000", "473.25"),
        question=question,
        sources=[live_source],
    ) == ""
    for modifier in ("current", "live", "latest", "actual", "official", "real"):
        neutral_source = {
            "title": "BRK.B stock market data",
            "url": "https://market.example/berkshire",
            "excerpt": (
                f"BRK.B {modifier} stock price was 473.25 USD on NYSE at "
                "2026-07-20 12:10 UTC."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer.replace("710000", "473.25"),
            question=question,
            sources=[neutral_source],
        ) == "", modifier


def test_ticker_only_question_binds_adjacent_company_name_to_exact_id(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    cases = (
        ("AAPL", "Apple AAPL", "225.25", "Hypothetical"),
        ("AAPL", "Apple (AAPL)", "225.25", "Synthetic"),
        ("AAPL", "AAPL Apple", "225.25", "Fictional"),
        ("BRK.B", "Berkshire Hathaway BRK.B", "473.25", "Hypothetical"),
        ("BRK.B", "BRK.B Berkshire Hathaway", "473.25", "Synthetic"),
    )
    for ticker, identity, value, non_live_status in cases:
        question = f"What is the current {ticker} stock price?"
        url = f"https://market.example/{ticker.casefold()}"
        quote = (
            f"{identity} stock price was {value} USD on NYSE at "
            "2026-07-20 12:10 UTC."
        )
        source = {
            "title": f"{ticker} stock quote",
            "url": url,
            "excerpt": quote,
        }
        answer = f"{quote} Source: {url}"
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question=question,
            sources=[source],
        ) == "", identity

        non_live_quote = quote.replace(
            " stock price", f" {non_live_status} stock price", 1
        )
        non_live_source = {**source, "excerpt": non_live_quote}
        assert tools_module._web_answer_financial_synthesis_rejection(
            f"{non_live_quote} Source: {url}",
            question=question,
            sources=[non_live_source],
        ) == "unsupported_financial_number", (identity, non_live_status)


def test_negated_quote_value_cannot_ground_a_positive_answer(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    wrong_answer = (
        "BRK.B stock price was 710000 USD on NYSE at 2026-07-20 12:20 UTC. "
        "Source: https://market.example/berkshire"
    )
    correct_answer = wrong_answer.replace("710000", "473.25", 1)
    excerpts = (
        "BRK.B stock price was not 710000 USD on NYSE at "
        "2026-07-20 12:20 UTC; actual stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
        "BRK.B stock price: not 710000 USD on NYSE at "
        "2026-07-20 12:20 UTC, but actual stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
        "BRK.B stock price was ≠ 710000 USD on NYSE at "
        "2026-07-20 12:20 UTC; actual stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
        "BRK.B stock price was 473.25 rather than 710000 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
        "BRK.B stock price was never 710000 USD on NYSE at "
        "2026-07-20 12:20 UTC; BRK.B actual stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
        "BRK.B stock price was no 710000 USD on NYSE at "
        "2026-07-20 12:20 UTC; BRK.B actual stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
        "BRK.B stock price was not equal to 710000 USD on NYSE at "
        "2026-07-20 12:20 UTC; BRK.B actual stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
        "Цена акций BRK.B никогда не 710000 USD на NYSE at "
        "2026-07-20 12:20 UTC; BRK.B actual stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:20 UTC.",
    )
    for excerpt in excerpts:
        source = {
            "title": "BRK.B current stock price",
            "url": "https://market.example/berkshire",
            "excerpt": excerpt,
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            wrong_answer,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number", excerpt

    positive_source = {
        "title": "BRK.B current stock price",
        "url": "https://market.example/berkshire",
        "excerpt": (
            "BRK.B actual stock price was 473.25 USD on NYSE at "
            "2026-07-20 12:20 UTC."
        ),
    }
    assert tools_module._web_answer_financial_synthesis_rejection(
        correct_answer,
        question=question,
        sources=[positive_source],
    ) == ""


def test_every_numeric_market_field_is_a_typed_occurrence(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    question = "What is the current BRK.B stock price?"
    correct = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:10 UTC. "
        "Source: https://market.example/berkshire"
    )
    excerpts = (
        "BRK.B open was 710000 USD — stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:10 UTC.",
        "BRK.B dividend was 710000 USD, stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:10 UTC.",
        "BRK.B revenue was 710000 USD | stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:10 UTC.",
        "BRK.B EBITDA was 710000 USD / stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:10 UTC.",
        "BRK.B shares outstanding were 710000 shares (stock price was 473.25 USD "
        "on NYSE at 2026-07-20 12:10 UTC).",
        "BRK.B rank was (710000), stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:10 UTC.",
        "BRK.B class 1 stock price was 473.25 USD on NYSE at "
        "2026-07-20 12:10 UTC.",
    )
    for excerpt in excerpts:
        source = {
            "title": "BRK.B stock price and market data",
            "url": "https://market.example/berkshire",
            "excerpt": excerpt,
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            correct,
            question=question,
            sources=[source],
        ) == "", excerpt
        assert tools_module._web_answer_financial_synthesis_rejection(
            correct.replace("473.25", "710000", 1),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number", excerpt

    unavailable_price_fields = (
        "enterprise value",
        "turnover",
        "volatility",
        "spread",
        "net debt",
        "page views",
    )
    for field in unavailable_price_fields:
        source = {
            "title": "BRK.B stock market data",
            "url": "https://market.example/berkshire",
            "excerpt": (
                f"BRK.B {field} was 710000 USD on NYSE at "
                "2026-07-20 12:10 UTC — stock price unavailable."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            correct.replace("473.25", "710000", 1),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number", field

    header_fields = ("total assets", "beta", "cash balance", "employees")
    for field in header_fields:
        source = {
            "title": "BRK.B stock market data",
            "url": "https://market.example/berkshire",
            "excerpt": (
                f"Stock prices: BRK.B {field} was 710000 USD on NYSE at "
                "2026-07-20 12:10 UTC; current stock price unavailable."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            correct.replace("473.25", "710000", 1),
            question=question,
            sources=[source],
        ) == "unsupported_financial_number", field


def test_quote_timestamp_requires_a_direct_field_edge(monkeypatch):
    today = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: now)
    equity_question = "What is the current BRK.B stock price?"
    equity_answer = (
        "BRK.B stock price was 473.25 USD on NYSE at 2026-07-20 12:10 UTC. "
        "Source: https://market.example/quote"
    )
    borrowed_sources = (
        "BRK.B stock price was 473.25 USD, trading volume at "
        "2026-07-20 12:10 UTC was 710000 shares on NYSE.",
        "BRK.A stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC then "
        "BRK.B stock price was 473.25 USD on NYSE.",
        "BRK.A stock price was 710000 USD on NYSE at 2026-07-20 12:10 UTC | "
        "BRK.B stock price was 473.25 USD on NYSE.",
    )
    for excerpt in borrowed_sources:
        source = {
            "title": "BRK.A and BRK.B stock quotes",
            "url": "https://market.example/quote",
            "excerpt": excerpt,
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            equity_answer,
            question=equity_question,
            sources=[source],
        ) == "unsupported_financial_number", excerpt

    crypto_question = "What is the current Bitcoin price?"
    crypto_answer = (
        "Bitcoin spot price was 65000 USD on exchange at 2026-07-20 12:10 UTC. "
        "Source: https://market.example/quote"
    )
    for metadata_prefix in (
        "Feed timestamp",
        "Snapshot generated at",
        "Page last refreshed at",
    ):
        source = {
            "title": "Bitcoin spot price",
            "url": "https://market.example/quote",
            "excerpt": (
                f"{metadata_prefix} 2026-07-20 12:10 UTC, Bitcoin spot price was "
                "65000 USD on exchange."
            ),
        }
        assert tools_module._web_answer_financial_synthesis_rejection(
            crypto_answer,
            question=crypto_question,
            sources=[source],
        ) == "unsupported_financial_number", metadata_prefix


def test_bond_yield_and_coupon_percent_are_primary_typed_values(monkeypatch):
    today = date(2026, 7, 20)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    isin = "US0378331005"
    for metric, value in (("yield", "4.25"), ("coupon", "5")):
        question = f"What is the current {metric} for bond ISIN {isin}?"
        source = {
            "title": f"Bond {metric} for ISIN {isin}",
            "url": "https://market.example/bond",
            "excerpt": (
                f"Bond ISIN {isin} current {metric} was {value} percent OTC on "
                "2026-07-20."
            ),
        }
        answer = (
            f"Bond ISIN {isin} current {metric} was {value} percent OTC on "
            "2026-07-20. Source: https://market.example/bond"
        )
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question=question,
            sources=[source],
        ) == ""


def test_generic_metric_adjectives_are_not_lowercase_ticker_subjects():
    assert tools_module._web_answer_financial_exact_identifiers(
        "average price, closing price, target price, fair price, adjusted price"
    ) == set()
    assert tools_module._web_answer_financial_exact_identifiers(
        "Stock prices: BRK.A stock price"
    ) == {"BRK.A"}
    assert tools_module._web_answer_financial_exact_identifiers(
        "BRK.B live stock price"
    ) == {"BRK.B"}


def test_invalid_financial_timezone_offsets_fail_closed(monkeypatch):
    today = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_news_today", lambda now=None: today)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: now)

    assert len(
        tools_module._web_answer_financial_timestamp_spans(
            "Bitcoin price 65000 USD at 2026-07-20 12:20 +03:00"
        )
    ) == 1
    for offset in ("+24:00", "+99:99", "+01:60"):
        assert tools_module._web_answer_financial_timestamp_spans(
            f"Bitcoin price 65000 USD at 2026-07-20 12:20 {offset}"
        ) == []
        source = {
            "title": "Bitcoin BTC/USD live spot quote",
            "url": "https://crypto.example/btcusd",
            "excerpt": (
                "Bitcoin BTC/USD spot price was 65000 USD at "
                "2026-07-20 12:20 UTC."
            ),
        }
        answer = (
            "Bitcoin BTC/USD spot exchange price was 65000 USD at "
            f"2026-07-20 12:20 {offset}. This is the current quote. "
            "Source: https://crypto.example/btcusd"
        )
        assert tools_module._web_answer_financial_synthesis_rejection(
            answer,
            question="What is the current Bitcoin price?",
            sources=[source],
        ) == "missing_quote_timestamp"


def test_crypto_value_requires_source_bound_fresh_timestamp_and_timezone(monkeypatch):
    fixed_now = datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: fixed_now)
    question = "What is the current Bitcoin price?"
    source = {
        "title": "Bitcoin BTC/USD spot exchange quote",
        "url": "https://crypto.example/btcusd",
        "excerpt": "Bitcoin BTC/USD spot price was 65000 USD at 2026-07-19 12:20 UTC.",
    }
    grounded = (
        "Bitcoin BTC/USD spot exchange price was 65000 USD at 2026-07-19 12:20 UTC. "
        "This is the current quote. Source: https://crypto.example/btcusd"
    )
    assert tools_module._web_answer_synthesis_rejection(
        grounded, [source], question=question
    ) == ""
    assert tools_module._web_answer_synthesis_rejection(
        grounded.replace(" at 2026-07-19 12:20 UTC", " on 2026-07-19"),
        [source],
        question=question,
    ) == "missing_quote_timestamp"
    assert tools_module._web_answer_synthesis_rejection(
        grounded.replace("12:20 UTC", "12:29 UTC"), [source], question=question
    ) == "unsupported_financial_number"
    assert tools_module._web_answer_synthesis_rejection(
        grounded.replace("2026-07-19 12:20 UTC", "2026-07-18 23:59 UTC"),
        [source],
        question=question,
    ) == "stale_financial_quote"
    assert tools_module._web_answer_synthesis_rejection(
        grounded.replace("12:20 UTC", "12:20"),
        [source],
        question=question,
    ) == "missing_quote_timestamp"


def test_crypto_rfc3339_timestamps_with_fractional_seconds_are_grounded(monkeypatch):
    fixed_now = datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: fixed_now)
    question = "What is the current Bitcoin price?"

    for timestamp, expected_microsecond in (
        ("2026-07-19T12:20:00Z", 0),
        ("2026-07-19T12:20:00.123456789Z", 123456),
    ):
        source = {
            "title": "Bitcoin BTC/USD live spot exchange quote",
            "url": "https://crypto.example/btcusd",
            "excerpt": f"Bitcoin BTC/USD spot price was 65000 USD at {timestamp}.",
        }
        answer = (
            f"Bitcoin BTC/USD spot exchange price was 65000 USD at {timestamp}. "
            "This is the current quote. Source: https://crypto.example/btcusd"
        )
        parsed = tools_module._web_answer_financial_timestamp_spans(timestamp)

        assert len(parsed) == 1
        assert parsed[0][0] == datetime(
            2026,
            7,
            19,
            12,
            20,
            tzinfo=UTC,
            microsecond=expected_microsecond,
        )
        assert tools_module._web_answer_synthesis_rejection(
            answer,
            [source],
            question=question,
        ) == ""


def test_crypto_value_and_timestamp_must_share_the_same_quote(monkeypatch):
    fixed_now = datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: fixed_now)
    question = "What is the current Bitcoin price?"
    separators = (". ", " and ", " и ", "; ", "\n")

    for separator in separators:
        source = {
            "title": "Bitcoin BTC/USD live spot quotes",
            "url": "https://crypto.example/btcusd",
            "excerpt": (
                "Bitcoin BTC/USD spot price was 65000 USD at 2026-07-19 12:20 UTC"
                f"{separator}Bitcoin BTC/USD spot price was 66000 USD at "
                "2026-07-19 12:25 UTC."
            ),
        }
        grounded = (
            "Bitcoin BTC/USD spot exchange price was 65000 USD at "
            "2026-07-19 12:20 UTC. Source: https://crypto.example/btcusd"
        )
        borrowed_timestamp = grounded.replace("12:20 UTC", "12:25 UTC")

        assert tools_module._web_answer_financial_synthesis_rejection(
            grounded,
            question=question,
            sources=[source],
        ) == ""
        assert tools_module._web_answer_financial_synthesis_rejection(
            borrowed_timestamp,
            question=question,
            sources=[source],
        ) == "unsupported_financial_number"


def test_crypto_previous_calendar_day_is_not_live_even_within_two_hours(monkeypatch):
    fixed_now = datetime(
        2026,
        7,
        19,
        0,
        30,
        tzinfo=tools_module.WEB_NEWS_TIMEZONE,
    )
    monkeypatch.setattr(tools_module, "_web_answer_financial_now", lambda: fixed_now)
    question = "What is the current Bitcoin price?"
    source = {
        "title": "Bitcoin BTC/USD live spot quote",
        "url": "https://crypto.example/btcusd",
        "excerpt": (
            "Bitcoin BTC/USD spot price was 65000 USD at "
            "2026-07-18 23:59 MSK."
        ),
    }
    answer = (
        "Bitcoin BTC/USD spot exchange price was 65000 USD at "
        "2026-07-18 23:59 MSK. Source: https://crypto.example/btcusd"
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        answer,
        question=question,
        sources=[source],
    ) == "stale_financial_quote"


class _FailingFinancialTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, name: str):
        return object() if name == "web.answer" else None

    async def run(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return ToolRunResponse(
            tool=name,
            ok=False,
            summary="provider offline",
            data={},
        )


def test_financial_provider_failure_never_falls_back_to_model_memory():
    runtime = object.__new__(agent_module.AgentRuntime)
    runtime.tools = _FailingFinancialTools()

    action = asyncio.run(
        runtime._run_web_answer_engine(
            message="Какие цены на нефть сейчас?",
            query="Какие цены на нефть сейчас?",
            conversation_id=None,
        )
    )

    assert action is not None
    assert "Не удалось подтвердить свежую рыночную котировку" in action.answer
    assert "Brent/WTI" in action.answer
    assert "нет доступа к данным" not in action.answer.casefold()
    assert runtime.tools.calls == [
        (
            "web.answer",
            {
                "question": "Какие цены на нефть сейчас?",
                "query": "Какие цены на нефть сейчас?",
                "max_sources": 6,
                "freshness": "day",
                "use_cache": False,
            },
        )
    ]


def test_fx_provider_failure_does_not_ask_for_brent_or_ticker():
    runtime = object.__new__(agent_module.AgentRuntime)
    runtime.tools = _FailingFinancialTools()

    action = asyncio.run(
        runtime._run_web_answer_engine(
            message="курс рубля к доллару",
            query="курс рубля к доллару",
            conversation_id=None,
        )
    )

    assert action is not None
    assert "валютн" in action.answer.casefold()
    assert "Brent" not in action.answer
    assert "WTI" not in action.answer
    assert "тикер" not in action.answer.casefold()
    assert "USD/RUB" in action.answer or "валютную пару" in action.answer.casefold()


def test_cbr_fx_rate_helpers_and_answer_format():
    valute = {
        "USD": {"CharCode": "USD", "Nominal": 1, "Value": 78.5},
        "EUR": {"CharCode": "EUR", "Nominal": 1, "Value": 85.0},
    }
    assert tools_module._web_answer_cbr_pair_rate(valute, base="USD", quote="RUB") == 78.5
    rub_usd = tools_module._web_answer_cbr_pair_rate(valute, base="RUB", quote="USD")
    assert rub_usd is not None and abs(rub_usd - (1 / 78.5)) < 1e-9
    assert tools_module._web_answer_financial_instrument_kind("курс рубля к доллару") == "fx"
    # Local convention: when RUB is one of the legs, foreign is base (USD/RUB),
    # so "курс рубля к доллару" and "300 долларов в рублях" share the same pair.
    assert tools_module._web_answer_requested_currency_pairs("курс рубля к доллару") == [
        ("USD", "RUB")
    ]
    assert tools_module._web_answer_requested_currency_pairs(
        "Сколько рублей в 300 долларах?"
    ) == [("USD", "RUB")]
    assert tools_module._web_answer_fx_money_amount("Сколько рублей в 300 долларах?") == (
        300.0,
        "USD",
    )
    source = {
        "url": "https://www.cbr-xml-daily.ru/daily_json.js",
        "title": "CBR official FX rate USD/RUB",
        "excerpt": (
            "Official Bank of Russia daily FX table: currency pair USD/RUB "
            "rate is 78.5 RUB per 1 USD on 2026-07-18."
        ),
        "market_quote": {
            "instrument_type": "FX",
            "provider": "cbr",
            "base": "USD",
            "quote": "RUB",
            "price": "78.5",
            "quote_date": "2026-07-18",
        },
    }
    answer = tools_module._format_fx_provider_answer("курс рубля к доллару", [source])
    assert "USD/RUB" in answer
    assert "78.5" in answer
    assert "Банка России" in answer
    converted = tools_module._format_fx_provider_answer(
        "Сколько рублей в 300 долларах?", [source]
    )
    assert "300 USD" in converted
    assert "23,550.00 RUB" in converted or "23550" in converted.replace(",", "")
    # FX uses its own grounding path — oil/futures contract must not reject it.
    assert tools_module._web_answer_fx_answer_is_grounded(
        answer,
        question="курс рубля к доллару",
        sources=[source],
    )


def test_fx_failure_path_never_mentions_brent_when_web_answer_fails():
    """Regression: currency questions used the oil fail-closed copy."""

    assert agent_module._financial_failure_instrument_kind("курс рубля к доллару") == "fx"
    assert agent_module._financial_failure_instrument_kind("цены на нефть Brent") in {
        "crude",
        "futures",
    }


def test_financial_web_answer_bypasses_answer_cache_on_every_turn(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def fake_research(_ctx, args):
        calls.append(dict(args))
        return ToolRunResponse(
            tool="web.research",
            ok=True,
            summary="live provider called",
            data={
                "sources": [
                    {
                        "title": "Brent crude oil latest settlement",
                        "url": "https://market.example/brent",
                        "excerpt": (
                            "Brent futures settlement 70.25 USD per barrel "
                            "on 2026-07-17."
                        ),
                        "fetched": True,
                    }
                ]
            },
        )

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(tools_module, "_web_research", fake_research)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    registry = ToolRegistry(settings, storage, SimpleNamespace())
    try:
        first = asyncio.run(
            registry.run("web.answer", {"question": "Какие цены на нефть сейчас?"})
        )
        calls_after_first = len(calls)
        second = asyncio.run(
            registry.run("web.answer", {"question": "Какие цены на нефть сейчас?"})
        )
    finally:
        storage.close()

    assert first.ok and second.ok
    assert calls_after_first > 0
    assert len(calls) == calls_after_first * 2
    assert all(call["use_cache"] is False for call in calls)
    assert all(call["archive_fallback"] is False for call in calls)
    assert first.data["cache"] == {"hit": False, "enabled": False, "ttl_sec": 600}
    assert second.data["cache"] == first.data["cache"]


def test_financial_web_answer_fails_closed_when_only_quote_is_stale(monkeypatch, tmp_path):
    async def fake_research(_ctx, _args):
        return ToolRunResponse(
            tool="web.research",
            ok=True,
            summary="old market page",
            data={
                "sources": [
                    {
                        "title": "Brent historical settlement",
                        "url": "https://market.example/brent-old",
                        "excerpt": (
                            "Brent futures settlement was 74.00 USD per barrel "
                            "on 2024-01-05."
                        ),
                        "fetched": True,
                    }
                ]
            },
        )

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(tools_module, "_web_research", fake_research)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    registry = ToolRegistry(settings, storage, SimpleNamespace())
    try:
        result = asyncio.run(
            registry.run("web.answer", {"question": "Какие цены на нефть сейчас?"})
        )
    finally:
        storage.close()

    assert result.ok is False
    assert result.data["financial_contract"]["accepted"] is False
    assert result.data["synthesis"]["reason"] == "financial_contract_rejected"
    assert "74.00" not in result.data["answer"]
    assert "Устаревшие или смешанные данные не выдаю" in result.data["answer"]


def test_web_research_propagates_no_cache_to_fetch_and_disables_archive(monkeypatch, tmp_path):
    fetch_calls: list[dict] = []

    async def fake_search(_ctx, _args):
        return ToolRunResponse(
            tool="web.search",
            ok=True,
            summary="live search",
            data={
                "results": [
                    {
                        "rank": 1,
                        "title": "Brent quote",
                        "url": "https://market.example/brent",
                        "snippet": "Brent market quote",
                        "vertical": "web",
                    }
                ]
            },
        )

    async def fake_fetch(_ctx, args):
        fetch_calls.append(dict(args))
        return ToolRunResponse(
            tool="web.fetch",
            ok=False,
            summary="blocked live page",
            data={"url": args["url"], "text": "", "blocked": True},
        )

    async def archive_must_not_run(*_args, **_kwargs):
        raise AssertionError("archive fallback must be disabled for live quotes")

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(tools_module, "_web_search", fake_search)
    monkeypatch.setattr(tools_module, "_web_fetch", fake_fetch)
    monkeypatch.setattr(tools_module, "_web_archive", archive_must_not_run)
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
                    "query": "Brent current quote",
                    "claim": "Какие цены на нефть сейчас?",
                    "freshness": "day",
                    "use_cache": False,
                    "archive_fallback": False,
                    "max_sources": 2,
                },
            )
        )
    finally:
        storage.close()

    assert result.ok is True
    assert fetch_calls
    assert all(call["use_cache"] is False for call in fetch_calls)


def test_same_sentence_brent_and_wti_values_keep_their_own_benchmark():
    source = {
        "title": "Brent and WTI latest settlements",
        "url": "https://market.example/oil",
        "excerpt": (
            "On 2026-07-17 Brent latest settlement is 70.25 USD per barrel and WTI "
            "latest settlement is 65.12 USD per barrel."
        ),
    }
    grounded = (
        "On 2026-07-17 Brent latest settlement is 70.25 USD per barrel and WTI latest "
        "settlement is 65.12 USD per barrel. Source: https://market.example/oil"
    )
    swapped = grounded.replace("70.25", "TMP").replace("65.12", "70.25").replace(
        "TMP", "65.12"
    )
    question = "What are the current Brent and WTI prices?"

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=[source],
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        swapped,
        question=question,
        sources=[source],
    ) == "unsupported_financial_number"


def test_same_sentence_equity_values_cannot_be_swapped_between_companies():
    source = {
        "title": "Apple and Microsoft stock quotes",
        "url": "https://market.example/mega-cap",
        "excerpt": (
            "Apple AAPL stock on NASDAQ was 225.25 USD on 2026-07-17 and Microsoft "
            "MSFT stock on NASDAQ was 510.50 USD on 2026-07-17."
        ),
    }
    swapped = (
        "Apple AAPL stock on NASDAQ was 510.50 USD on 2026-07-17 and Microsoft MSFT "
        "stock on NASDAQ was 225.25 USD on 2026-07-17. "
        "Source: https://market.example/mega-cap"
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        swapped,
        question="What are the current Apple and Microsoft stock prices?",
        sources=[source],
    ) == "unsupported_financial_number"


def test_each_etf_and_bond_value_keeps_its_own_metric():
    etf_source = {
        "title": "SPY ETF price and NAV",
        "url": "https://market.example/spy",
        "excerpt": (
            "SPY ETF market price was 630.00 USD on NYSE on 2026-07-17 and NAV was "
            "629.00 USD on NYSE on 2026-07-17."
        ),
    }
    bond_source = {
        "title": "OFZ bond price and yield",
        "url": "https://market.example/ofz",
        "excerpt": (
            "OFZ 26238 bond price was 98.50 RUB on MOEX on 2026-07-17 and current yield "
            "was 4.25 percent on MOEX on 2026-07-17."
        ),
    }

    assert tools_module._web_answer_financial_synthesis_rejection(
        (
            "SPY ETF NAV was 630.00 USD on NYSE on 2026-07-17. "
            "Source: https://market.example/spy"
        ),
        question="What is the current NAV of SPY ETF?",
        sources=[etf_source],
    ) == "unsupported_financial_number"
    assert tools_module._web_answer_financial_synthesis_rejection(
        (
            "OFZ 26238 bond current yield was 98.50 percent on MOEX on 2026-07-17. "
            "Source: https://market.example/ofz"
        ),
        question="What is the current yield of OFZ 26238 bond?",
        sources=[bond_source],
    ) == "unsupported_financial_number"


def test_english_stock_price_is_not_mistaken_for_the_ice_oil_venue():
    question = "What is the current Apple stock price?"
    source = {
        "title": "Apple AAPL stock quote",
        "url": "https://market.example/aapl",
        "excerpt": "Apple AAPL stock on NASDAQ last traded at 225.25 USD on 2026-07-17.",
    }
    answer = (
        "Apple AAPL stock on NASDAQ last traded at 225.25 USD on 2026-07-17. "
        "Source: https://market.example/aapl"
    )

    assert tools_module._web_answer_looks_like_financial_market(question.casefold())
    assert tools_module._web_answer_financial_synthesis_rejection(
        answer,
        question=question,
        sources=[source],
    ) == ""


def test_current_exchange_quote_requires_latest_available_weekday_session():
    today = date(2026, 7, 19)  # Sunday; Friday the 17th is the latest session.

    assert tools_module._web_answer_financial_date_is_fresh(
        date(2026, 7, 17), kind="equity", today=today
    )
    assert not tools_module._web_answer_financial_date_is_fresh(
        date(2026, 7, 16), kind="equity", today=today
    )
    assert not tools_module._web_answer_financial_date_is_fresh(
        date(2026, 7, 20), kind="equity", today=today
    )


def test_exchange_freshness_uses_holiday_calendar_and_exchange_session_date():
    # Monday 2026-01-19 is MLK Day.  Before Tuesday's session opens, Friday is
    # still the latest completed scheduled US exchange session.
    assert tools_module._web_answer_financial_date_is_fresh(
        date(2026, 1, 16),
        kind="crude",
        today=date(2026, 1, 20),
    )
    # On an ordinary Sunday, Thursday is stale because Friday traded.
    assert not tools_module._web_answer_financial_date_is_fresh(
        date(2026, 1, 15),
        kind="equity",
        today=date(2026, 1, 18),
    )


def test_cash_index_source_rejects_index_futures_contract():
    question = "What is the current S&P 500 index level?"
    source_text = (
        "S&P 500 index futures contract latest level is 6550.25 points today."
    )

    assert tools_module._web_answer_financial_instrument_kind(question) == "index"
    assert not tools_module._web_answer_financial_source_relevant(
        question,
        source_text,
        kind="index",
    )


def test_fx_value_cannot_be_borrowed_from_another_pair_in_the_same_sentence():
    source = {
        "title": "USD/RUB and EUR/USD live forex quotes",
        "url": "https://fx.example/live",
        "excerpt": (
            "On 2026-07-17 USD/RUB latest forex quote was 90.25 and EUR/USD latest "
            "forex quote was 1.08."
        ),
    }
    answer = (
        "USD/RUB latest forex quote is 1.08 RUB per USD on 2026-07-17. "
        "Source: https://fx.example/live"
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        answer,
        question="What is the current USD/RUB exchange rate?",
        sources=[source],
    ) == "unsupported_financial_number"


_CFTC_CRUDE_UNIT_URL = (
    "https://www.cftc.gov/filings/orgrules/rule020217nymexdcm003.pdf"
)


def _typed_crude_quote_source(
    symbol: str,
    benchmark: str,
    price: str,
    quote_time: str,
) -> dict[str, object]:
    quote_url = f"https://quotes.example/{symbol.casefold().replace('=', '-')}"
    return {
        "title": f"{benchmark.title()} {symbol} typed quote",
        "url": quote_url,
        "market_quote": {
            "benchmark": benchmark,
            "symbol": symbol,
            "instrument_type": "FUTURE",
            "instrument_alias": "undated_provider_symbol",
            "is_dated_contract": False,
            "exchange": "NYM",
            "currency": "USD",
            "unit": "per barrel",
            "unit_source_url": _CFTC_CRUDE_UNIT_URL,
            "price": price,
            "quote_time_utc": quote_time,
        },
    }


def _structured_crude_quote_answer(
    *,
    brent_price: str = "88.1",
    wti_price: str = "81.78",
    brent_time: str = "2026-07-17T20:59:57Z",
    wti_time: str = "2026-07-17T20:59:58Z",
    extra_field: str = "",
) -> str:
    return (
        "As of today, 2026-07-19, these are the latest available quotes.\n\n"
        "## Brent — BZ=F\n"
        "- Instrument: futures; undated provider symbol, not a specific dated contract.\n"
        "- Exchange: NYM.\n"
        f"{extra_field}"
        f"- Price: {brent_price} USD per barrel.\n"
        f"- Quote time: {brent_time}.\n"
        "- Source: https://quotes.example/bz-f\n\n"
        "## WTI — CL=F\n"
        "- Instrument: futures; undated provider symbol, not a specific dated contract.\n"
        "- Exchange: NYM.\n"
        f"{extra_field}"
        f"- Price: {wti_price} USD per barrel.\n"
        f"- Quote time: {wti_time}.\n"
        "- Source: https://quotes.example/cl-f\n\n"
        f"Official unit source: {_CFTC_CRUDE_UNIT_URL}"
    )


def test_typed_quotes_ground_markdown_cards_on_weekend_without_today_relabel():
    sources = [
        _typed_crude_quote_source(
            "BZ=F", "brent", "88.1", "2026-07-17T20:59:57Z"
        ),
        _typed_crude_quote_source(
            "CL=F", "wti", "81.78", "2026-07-17T20:59:58Z"
        ),
    ]
    question = "BZ=F CL=F futures price today 2026-07-19"

    assert tools_module._web_answer_financial_exact_identifiers(question) == {
        "BZ=F",
        "CL=F",
    }
    assert tools_module._web_answer_financial_synthesis_rejection(
        _structured_crude_quote_answer(),
        question=question,
        sources=sources,
    ) == ""
    tampered = _typed_crude_quote_source(
        "BZ=F", "wti", "88.1", "2026-07-17T20:59:57Z"
    )
    assert tools_module._web_answer_financial_market_quote_tuple(tampered) == ""


def test_structured_typed_quotes_reject_swaps_and_quote_time_relabelling():
    sources = [
        _typed_crude_quote_source(
            "BZ=F", "brent", "88.1", "2026-07-17T20:59:57Z"
        ),
        _typed_crude_quote_source(
            "CL=F", "wti", "81.78", "2026-07-17T20:59:58Z"
        ),
    ]
    question = "BZ=F CL=F futures price today 2026-07-19"

    assert tools_module._web_answer_financial_synthesis_rejection(
        _structured_crude_quote_answer(brent_price="81.78", wti_price="88.1"),
        question=question,
        sources=sources,
    ) == "unsupported_financial_number"
    assert tools_module._web_answer_financial_synthesis_rejection(
        _structured_crude_quote_answer(brent_time="2026-07-18T20:59:57Z"),
        question=question,
        sources=sources,
    ) == "unsupported_financial_number"


def test_historical_typed_quote_keeps_exact_requested_date_binding():
    source = _typed_crude_quote_source(
        "BZ=F", "brent", "79.25", "2026-07-16T20:59:57Z"
    )
    question = "BZ=F futures price on 2026-07-16"
    grounded = _structured_crude_quote_answer(
        brent_price="79.25",
        brent_time="2026-07-16T20:59:57Z",
        wti_price="79.25",
        wti_time="2026-07-16T20:59:57Z",
    ).split("## WTI", maxsplit=1)[0] + f"Official unit source: {_CFTC_CRUDE_UNIT_URL}"

    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded,
        question=question,
        sources=[source],
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        grounded.replace("2026-07-16T20:59:57Z", "2026-07-17T20:59:57Z"),
        question=question,
        sources=[source],
    ) == "requested_quote_date_mismatch"


def test_undated_typed_quote_rejects_positive_front_month_claim_with_disclosure():
    sources = [
        _typed_crude_quote_source(
            "BZ=F", "brent", "88.1", "2026-07-17T20:59:57Z"
        ),
        _typed_crude_quote_source(
            "CL=F", "wti", "81.78", "2026-07-17T20:59:58Z"
        ),
    ]
    answer = _structured_crude_quote_answer().replace(
        "not a specific dated contract.",
        "not a specific dated contract; this is a continuous/front-month futures alias.",
    )

    assert tools_module._web_answer_financial_synthesis_rejection(
        answer,
        question="BZ=F CL=F futures price today 2026-07-19",
        sources=sources,
    ) == "unsupported_futures_alias"


def test_contract_month_example_is_metadata_not_a_quote_value():
    sources = [
        _typed_crude_quote_source(
            "BZ=F", "brent", "88.1", "2026-07-17T20:59:57Z"
        ),
        _typed_crude_quote_source(
            "CL=F", "wti", "81.78", "2026-07-17T20:59:58Z"
        ),
    ]

    assert tools_module._web_answer_financial_synthesis_rejection(
        _structured_crude_quote_answer(
            extra_field="- Contract-month example only: August 2026.\n"
        ),
        question="BZ=F CL=F futures price today 2026-07-19",
        sources=sources,
    ) == ""


def _plain_qwen_style_crude_quote_answer(
    *,
    brent_price: str = "88.1",
    wti_price: str = "81.78",
    brent_time: str = "2026-07-17T20:59:57Z",
    wti_time: str = "2026-07-17T20:59:58Z",
) -> str:
    """Qwen-style cards without Markdown AT headings (real model layout)."""

    return (
        "As of today, 2026-07-19, these are the latest available quotes.\n\n"
        "Brent (BZ=F)\n"
        "- Instrument: futures; undated provider symbol, not a specific dated contract.\n"
        "- Exchange: NYM.\n"
        f"- Price: {brent_price} USD per barrel.\n"
        f"- Quote time: {brent_time}.\n"
        "- Source: https://quotes.example/bz-f\n\n"
        "WTI (CL=F)\n"
        "- Instrument: futures; undated provider symbol, not a specific dated contract.\n"
        "- Exchange: NYM.\n"
        f"- Price: {wti_price} USD per barrel.\n"
        f"- Quote time: {wti_time}.\n"
        "- Source: https://quotes.example/cl-f\n\n"
        f"Official unit source: {_CFTC_CRUDE_UNIT_URL}"
    )


def test_typed_quotes_ground_plain_qwen_cards_without_hash_headings():
    sources = [
        _typed_crude_quote_source(
            "BZ=F", "brent", "88.1", "2026-07-17T20:59:57Z"
        ),
        _typed_crude_quote_source(
            "CL=F", "wti", "81.78", "2026-07-17T20:59:58Z"
        ),
    ]
    question = "BZ=F CL=F futures price today 2026-07-19"

    assert tools_module._web_answer_financial_synthesis_rejection(
        _plain_qwen_style_crude_quote_answer(),
        question=question,
        sources=sources,
    ) == ""
    assert tools_module._web_answer_financial_synthesis_rejection(
        _plain_qwen_style_crude_quote_answer(brent_price="81.78", wti_price="88.1"),
        question=question,
        sources=sources,
    ) == "unsupported_financial_number"
