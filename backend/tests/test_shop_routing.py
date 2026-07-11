"""Chat routing of shop-specific price queries to web.shop_search (not web.answer)."""

from __future__ import annotations

import asyncio

from jarvis_gpt.agent import (
    AgentRuntime,
    _clean_shopping_subject,
    _format_shop_search_answer,
    _looks_like_shopping_query,
    _ranking_criterion_from_message,
    _shop_key_from_message,
    _shop_search_url_for,
    _shopping_cities_from_message,
    _shopping_constraints_from_message,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


def _agent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus()
    )
    return agent, storage


_RANKED = {
    "ok": True,
    "shop": "dns",
    "city": "Москва",
    "count": 2,
    "cheapest": {
        "title": "Palit RTX 5090 GameRock OC",
        "url": "https://www.dns-shop.ru/product/def/",
        "price_text": "409 999 ₽",
        "price_value": 409999.0,
    },
    "items": [
        {
            "title": "Palit RTX 5090 GameRock OC",
            "url": "https://www.dns-shop.ru/product/def/",
            "price_text": "409 999 ₽",
            "price_value": 409999.0,
        },
        {
            "title": "ASUS RTX 5090 ROG Astral",
            "url": "https://www.dns-shop.ru/product/ghi/",
            "price_text": "499 999 ₽",
            "price_value": 499999.0,
        },
    ],
}


def test_shop_key_and_helpers():
    assert _shop_key_from_message("найди самую дешёвую 5090 на днс") == "dns"
    assert _shop_key_from_message("сколько стоит rtx 5090 на ozon") == "ozon"
    assert _shop_key_from_message("какая погода в казани") is None
    assert _shop_search_url_for("dns", "rtx 5090").startswith("https://www.dns-shop.ru/search/?q=")
    assert _shop_search_url_for("unknown", "x") == ""


def test_wildberries_superlative_is_a_structured_catalog_request():
    message = "а какой самый мощный лазер есть на вайлдберрис?"

    assert _looks_like_shopping_query(message) is True
    assert _shop_key_from_message(message) == "wildberries"
    assert _clean_shopping_subject(message) == "лазер"
    assert _ranking_criterion_from_message(message) == "power_desc"


def test_store_mentions_without_catalog_intent_stay_in_general_research():
    for message in (
        "Почему Wildberries сменил логотип?",
        "Как удалить аккаунт на Wildberries?",
        "Какие условия возврата на Ozon?",
        "Где ближайший пункт выдачи ВБ?",
        "Почему Ozon не работает?",
        "Официальный сайт Ozon",
        "Курс акций Ozon",
        "Как настроить DNS на роутере?",
        "Что такое DNS over HTTPS?",
        "Найди новости Ozon",
        "Найди вакансии в Ozon",
        "Как быстро доставляет Ozon?",
        "Адрес магазина DNS в Казани",
        "Самый большой склад Ozon",
        "Ozon работает быстрее Wildberries?",
    ):
        assert _looks_like_shopping_query(message.casefold()) is False, message


def test_terse_named_shop_product_queries_route_to_catalog():
    for message in (
        "лего на озоне",
        "какой лазер на WB",
        "наушники в Ситилинке",
        "видеокарта в Регарде",
        "iphone на Авито",
        "пылесос на Алиэкспресс",
        "найди лазер на Ozon и Wildberries",
        "сравни лазер на Ozon и Wildberries",
    ):
        assert _looks_like_shopping_query(message.casefold()) is True, message


def test_price_constraints_do_not_capture_product_specs():
    assert _shopping_constraints_from_message(
        "лазер до 3 000 рублей на Wildberries"
    ) == {"max_price": 3000.0}
    assert _shopping_constraints_from_message(
        "лазер дальностью до 500 метров на Wildberries"
    ) == {}
    assert _shopping_constraints_from_message(
        "пауэрбанк ёмкостью от 20000 мАч на Ozon"
    ) == {}
    assert _shopping_constraints_from_message("SSD скоростью от 7000 МБ/с на Ozon") == {}
    assert _shopping_constraints_from_message("пылесос рейтинг от 4.5 на Ozon") == {
        "min_rating": 4.5
    }
    assert _clean_shopping_subject("пылесос с рейтингом от 4.5 на Ozon") == "пылесос"


def test_constraints_and_delivery_location_are_removed_from_catalog_subject():
    message = "самый мощный лазер до 3.000 рублей на вайлдберрис с доставкой в Казань"

    assert _clean_shopping_subject(message) == "лазер"
    assert _shopping_constraints_from_message(message) == {"max_price": 3000.0}
    assert _shopping_cities_from_message(message) == ["Казань"]


def test_ranking_clauses_do_not_narrow_the_catalog_query():
    cases = {
        "самый производительный ноутбук на Ozon": ("ноутбук", "power_desc"),
        "самые быстрые наушники на Ozon": ("наушники", "speed_desc"),
        "пылесос с лучшим рейтингом на Ozon": ("пылесос", "rating_desc"),
        "самый популярный пылесос по числу отзывов на Ozon": (
            "пылесос",
            "popularity_desc",
        ),
        "пауэрбанк с самой большой ёмкостью на Ozon": (
            "пауэрбанк",
            "capacity_desc",
        ),
        "рация с самым большим радиусом действия на Ozon": (
            "рация",
            "range_desc",
        ),
        "наушники с большим временем работы на Ozon": (
            "наушники",
            "runtime_desc",
        ),
        "где дешевле лазер: Wildberries или Ozon?": ("лазер", "price_asc"),
        "самый дорогой лазер на Wildberries": ("лазер", "price_desc"),
        "товар с максимальной ценой на Ozon": ("", "price_desc"),
        "самый молодой автор на Avito": ("автор", "age_asc"),
        "самый старый журнал на Avito": ("журнал", "age_desc"),
        "самый лёгкий ноутбук на Ozon": ("ноутбук", "weight_asc"),
        "самый тяжёлый ноутбук на Ozon": ("ноутбук", "weight_desc"),
        "самый компактный ноутбук на Ozon": ("ноутбук", "size_asc"),
        "самый большой телевизор на Ozon": ("телевизор", "size_desc"),
        "самый новый iPhone на Ozon": ("iPhone", "date_desc"),
        "последний iPhone на Ozon": ("iPhone", "date_desc"),
    }
    for message, (subject, criterion) in cases.items():
        assert _clean_shopping_subject(message) == subject
        assert _ranking_criterion_from_message(message) == criterion


def test_product_names_are_not_mistaken_for_ranking_criteria():
    assert _ranking_criterion_from_message("найди мини-печь на Ozon") is None
    assert _clean_shopping_subject("найди мини-печь на Ozon") == "мини-печь"
    assert _ranking_criterion_from_message("ёмкость для воды на Ozon") is None
    assert _clean_shopping_subject("ёмкость для воды на Ozon") == "ёмкость воды"


def test_city_is_only_treated_as_delivery_context():
    assert _shopping_cities_from_message("книга Москва на Ozon") == []
    assert _clean_shopping_subject("книга Москва на Ozon") == "книга Москва"
    assert _shopping_cities_from_message("iphone на Ozon в Санкт-Петербурге") == [
        "Санкт-Петербург"
    ]
    assert _clean_shopping_subject("iphone на Ozon в Санкт-Петербурге") == "iphone"


def test_format_shop_search_answer_lists_cheapest_first():
    answer = _format_shop_search_answer(_RANKED, "rtx 5090")
    assert "Самая дешёвая" in answer
    assert "409 999 ₽" in answer
    assert "Все варианты по возрастанию цены" in answer
    assert "для города: Москва" in answer
    # cheapest appears before the pricier card
    assert answer.index("409 999") < answer.index("499 999")


def test_format_non_price_comparison_refuses_unsupported_winner():
    data = {
        "items": [
            {
                "title": "Пылесос без указанной мощности",
                "url": "https://shop.example/1",
                "price_text": "10 000 ₽",
            }
        ],
        "cheapest": {
            "title": "Пылесос без указанной мощности",
            "url": "https://shop.example/1",
            "price_text": "10 000 ₽",
        },
        "best": None,
        "comparison": {
            "criterion": "power_desc",
            "criterion_label": "максимальная мощность",
            "metric_key": "",
            "complete": False,
            "compared_count": 0,
            "discovered_count": 1,
        },
    }

    answer = _format_shop_search_answer(data, "пылесос")

    assert "победителя не называю" in answer
    assert "не подменяю критерий ценой" in answer
    assert "Самая дешёвая" not in answer


def test_format_non_price_comparison_requires_complete_evidence():
    item = {
        "title": "Единственная карточка 5 W",
        "url": "https://shop.example/1",
        "metrics": {"power_w": {"value": 5.0, "text": "5 W", "unit": "W"}},
    }
    answer = _format_shop_search_answer(
        {
            "items": [item],
            "best": item,
            "comparison": {
                "criterion": "power_desc",
                "criterion_label": "максимальная мощность",
                "metric_key": "power_w",
                "metric_label": "мощность",
                "complete": False,
                "compared_count": 1,
                "discovered_count": 1,
                "best_metric": item["metrics"]["power_w"],
            },
        },
        "лазер",
    )

    assert "Самое высокое" not in answer
    assert "победителя не называю" in answer


def test_format_ascending_non_price_comparison_uses_lowest_wording():
    best = {
        "title": "Лёгкий ноутбук",
        "url": "https://shop.example/light",
        "metrics": {"mass_kg": {"value": 1.1, "text": "1,1 кг", "unit": "kg"}},
    }
    answer = _format_shop_search_answer(
        {
            "items": [best, {"title": "Тяжёлый ноутбук", "url": "heavy"}],
            "best": best,
            "comparison": {
                "criterion": "weight_asc",
                "metric_key": "mass_kg",
                "metric_label": "масса",
                "complete": True,
                "compared_count": 2,
                "discovered_count": 2,
                "best_metric": best["metrics"]["mass_kg"],
            },
        },
        "ноутбук",
    )

    assert "Самое низкое заявленное значение" in answer
    assert "Самое высокое" not in answer


def test_shopping_dns_query_routes_to_shop_search_not_web_answer(monkeypatch, tmp_path):
    # The routing hook is gated on the browser layer being installed; force it
    # available so the test exercises the routing regardless of the CI env.
    monkeypatch.setattr("jarvis_gpt.agent._web_surfer_available", lambda: True)
    agent, storage = _agent(monkeypatch, tmp_path)
    called: list[str] = []

    async def fake_run(name, arguments=None, **kwargs):
        called.append(name)
        if name == "web.shop_search":
            return ToolRunResponse(
                tool="web.shop_search", ok=True, summary="2 товара", data=_RANKED
            )
        raise AssertionError(f"web.shop_search must win; got call to {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_web_research(
            "Найди мне самую дешёвую 5090 на днс",
            "самая дешёвая rtx 5090 dns",
            conversation_id=storage.create_conversation("shop"),
        )
    )

    assert "web.shop_search" in called
    assert "web.answer" not in called  # the fallback engine was not reached
    assert "409 999 ₽" in action.answer
    assert "Самая дешёвая" in action.answer
    storage.close()


def test_wildberries_power_query_routes_to_typed_comparison(monkeypatch, tmp_path):
    monkeypatch.setattr("jarvis_gpt.agent._web_surfer_available", lambda: True)
    agent, storage = _agent(monkeypatch, tmp_path)
    captured = {}
    winner = {
        "title": "Лазерная указка 100000mW",
        "url": "https://www.wildberries.ru/catalog/596702194/detail.aspx",
        "price_text": "2 969 ₽",
        "price_value": 2969.0,
        "metrics": {
            "power_w": {"value": 100.0, "text": "100000mW", "unit": "W"}
        },
    }

    async def fake_run(name, arguments=None, **_kwargs):
        assert name == "web.shop_search"
        captured.update(arguments or {})
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="typed comparison",
            data={
                "ok": True,
                "shop": "wildberries",
                "city": "Москва",
                "items": [winner],
                "best": winner,
                "comparison": {
                    "criterion": "power_desc",
                    "criterion_label": "максимальная мощность/производительность",
                    "metric_key": "power_w",
                    "metric_label": "мощность",
                    "complete": True,
                    "compared_count": 1,
                    "discovered_count": 1,
                    "best_metric": winner["metrics"]["power_w"],
                },
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_web_research(
            "а какой самый мощный лазер есть на вайлдберрис?",
            "самый мощный лазер Wildberries",
        )
    )

    assert captured == {
        "query": "лазер",
        "shop": "wildberries",
        "criterion": "power_desc",
        "criterion_label": "максимальная мощность/производительность",
    }
    assert "100000mW" in action.answer
    assert "данные продавцов" in action.answer
    assert "Самая дешёвая" not in action.answer
    storage.close()


def test_explicit_multi_shop_query_compares_every_named_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr("jarvis_gpt.agent._web_surfer_available", lambda: True)
    agent, storage = _agent(monkeypatch, tmp_path)
    called: list[tuple[str, str]] = []

    async def fake_run(name, arguments=None, **_kwargs):
        assert name == "web.shop_search"
        shop = arguments["shop"]
        called.append((shop, arguments["query"]))
        price = 900.0 if shop == "wildberries" else 1000.0
        item = {
            "title": f"Лазер {shop}",
            "url": f"https://{shop}.example/product/1",
            "price_text": f"{int(price)} ₽",
            "price_value": price,
            "in_stock": True,
        }
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="one",
            data={
                "items": [item],
                "best": item,
                "cheapest": item,
                "comparison": {
                    "criterion": "price_asc",
                    "metric_key": "price_value",
                    "metric_label": "цена",
                    "complete": True,
                },
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_web_research(
            "где дешевле лазер: Wildberries или Ozon?",
            "лазер Wildberries Ozon",
        )
    )

    assert called == [("wildberries", "лазер"), ("ozon", "лазер")]
    assert "900 ₽" in action.answer
    assert "wildberries" in action.answer
    assert "ozon" in action.answer
    storage.close()


def test_shop_search_needs_install_gives_actionable_message(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)

    async def fake_run(name, arguments=None, **kwargs):
        assert name == "web.shop_search"
        return ToolRunResponse(
            tool="web.shop_search",
            ok=False,
            summary="Browser surfer is unavailable",
            data={"needs_install": True},
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_shop_search("найди дешёвую 5090 на днс", "dns", conversation_id=None)
    )
    assert action is not None
    assert "playwright install chromium" in action.answer
    assert "requirements-surfer.txt" in action.answer
    # honest actionable message, not the misleading "site returned no data"
    assert "dns-shop.ru/search" in action.answer
    storage.close()


def test_shop_search_soft_failure_stays_honest_and_does_not_fall_back(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        assert name == "web.shop_search"
        captured.update(arguments or {})
        return ToolRunResponse(
            tool="web.shop_search",
            ok=False,
            summary="anti-bot wall",
            data={"error": "anti-bot"},
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_shop_search("найди дешёвую 5090 на днс", "dns", conversation_id=None)
    )
    assert captured == {
        "query": "rtx 5090",
        "shop": "dns",
        "criterion": "price_asc",
        "criterion_label": "минимальная цена",
    }
    assert "anti-bot" in action.answer
    assert "не подменяю результат общим веб-поиском" in action.answer
    assert "dns-shop.ru/search" in action.answer
    storage.close()
