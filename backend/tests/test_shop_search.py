"""Coverage for the browser shop-search: catalog parse, price ranking, tool wiring."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from jarvis_gpt import tools as tools_module
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def _stub_playwright() -> None:
    """Let web_surfer import in CI where the real Playwright driver is absent."""

    try:
        import playwright.async_api  # noqa: F401
        return
    except ImportError:
        pass
    pw = types.ModuleType("playwright")
    pa = types.ModuleType("playwright.async_api")
    for name in ("Browser", "BrowserContext", "Page", "Response"):
        setattr(pa, name, type(name, (), {}))
    pa.async_playwright = lambda: None
    pa.Error = type("Error", (Exception,), {})
    pa.TimeoutError = type("TimeoutError", (Exception,), {})
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = pa


# web_surfer needs beautifulsoup4 for catalog parsing; skip cleanly if absent.
pytest.importorskip("bs4")
_stub_playwright()

from jarvis_gpt import web_surfer as ws  # noqa: E402

# web_surfer has resolved its imports; drop the stub (our stub has no __file__)
# so it does not pollute other tests' Playwright-availability detection.
if getattr(sys.modules.get("playwright"), "__file__", None) is None:
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.async_api", None)

_DNS_GRID = """
<html><body>
<div class="products-list">
  <div class="catalog-product">
    <a class="catalog-product__name" href="/product/abc/rtx-5090-gamerock/">
      Видеокарта Palit GeForce RTX 5090 GameRock
    </a>
    <div class="product-buy__price">413 999 ₽</div>
  </div>
  <div class="catalog-product">
    <a class="catalog-product__name" href="/product/def/rtx-5090-gamerock-oc/">
      Видеокарта Palit GeForce RTX 5090 GameRock OC
    </a>
    <div class="product-buy__price">409 999 ₽</div>
  </div>
  <div class="catalog-product">
    <a class="catalog-product__name" href="/product/ghi/asus-rtx-5090-rog-astral/">
      Видеокарта ASUS GeForce RTX 5090 ROG Astral
    </a>
    <div class="product-buy__price">499 999 ₽</div>
  </div>
  <nav><a href="/help/">Помощь и советы по выбору</a></nav>
</div></body></html>
"""


def test_shop_search_url_templates_and_aliases():
    assert ws.shop_search_url("днс", "rtx 5090").startswith("https://www.dns-shop.ru/search/?q=")
    assert "order=price" in ws.shop_search_url("dns", "rtx 5090")
    assert ws.shop_search_url("Озон", "rtx 5090").startswith("https://www.ozon.ru/search/")
    assert ws.shop_search_url("wb", "ssd").startswith("https://www.wildberries.ru/")
    assert ws.shop_search_url("неизвестныймаг", "x") == ""


def test_dns_price_sort_is_reapplied_after_redirect():
    redirected = "https://www.dns-shop.ru/search/?q=rtx+5090&order=popular"
    sorted_url = ws._shop_price_sorted_url("dns", redirected)

    assert "order=price" in sorted_url
    assert "stock=all" in sorted_url
    assert ws._shop_price_sort_confirmed("dns", sorted_url) is True
    assert ws._shop_price_sort_confirmed("dns", redirected) is False


def test_city_label_from_dns_cookie():
    assert ws._city_label_from_cookies([{"name": "city_path", "value": "moscow"}]) == "Москва"
    assert ws._city_label_from_cookies([{"name": "other", "value": "x"}]) == ""


def test_extract_catalog_items_from_dns_like_grid():
    items = ws._extract_catalog_items(_DNS_GRID, base_url="https://www.dns-shop.ru/search/?q=rtx")
    # Three products; the nav "Помощь" link has no nearby price and is excluded.
    assert len(items) == 3
    urls = {item["url"] for item in items}
    assert all("/product/" in url for url in urls)
    assert all("dns-shop.ru" in url for url in urls)
    values = sorted(item["price_value"] for item in items)
    assert values == [409999.0, 413999.0, 499999.0]


def test_dns_heuristic_parser_rejects_priced_catalog_recipe_links():
    html = """
    <div class="catalog-product">
      <a href="/catalog/recipe/rtx-5090/">RTX 5090</a><div>409 999 ₽</div>
    </div>
    <div class="catalog-product">
      <a href="/product/real/rtx-5090/">Видеокарта RTX 5090</a><div>413 999 ₽</div>
    </div>
    """

    items = ws._extract_catalog_items(
        html,
        base_url="https://www.dns-shop.ru/search/?q=rtx+5090",
    )

    assert [item["url"] for item in items] == [
        "https://www.dns-shop.ru/product/real/rtx-5090/"
    ]


def test_wildberries_parser_uses_real_product_cards_and_rejects_search_links():
    html = """
    <a href="/catalog/0/search.aspx?search=laser&nocorrection=1">«лазер»</a>
    <div>502 ₽</div>
    <article class="product-card" data-nm-id="721395131">
      <a href="/catalog/721395131/detail.aspx"
         aria-label="Мощнейшая лазерная указка 50 000 мВт"></a>
      <ins>3 026 ₽</ins>
      <span>4,8</span><span>50 оценок</span>
      <img src="https://basket-34.wbbasket.ru/vol7213/part721395/721395131/images/c516x688/1.webp">
    </article>
    """

    items = ws._extract_catalog_items(
        html,
        base_url="https://www.wildberries.ru/catalog/0/search.aspx?search=laser",
    )

    assert len(items) == 1
    assert items[0]["product_id"] == "721395131"
    assert items[0]["price_value"] == 3026.0
    assert items[0]["rating_value"] == 4.8
    assert items[0]["review_count"] == 50
    assert items[0]["url"].endswith("/catalog/721395131/detail.aspx")
    assert items[0]["details_url"].endswith("/721395131/info/ru/card.json")


def test_power_ranking_converts_units_and_ignores_range_numbers():
    items = [
        {"title": "Лазерная указка 5 W", "url": "five", "in_stock": True},
        {"title": "Лазерная указка 1000 mW", "url": "one", "in_stock": True},
        {"title": "Лазерная указка дальность 300 м", "url": "range", "in_stock": True},
        {"title": "Лазерная указка 50 000 мВт", "url": "fifty", "in_stock": True},
    ]
    for item in items:
        ws._attach_catalog_metrics(item)

    metric_key = ws._select_catalog_metric_key(items, "power_desc")
    ranked = ws._rank_catalog_items(
        items,
        criterion="power_desc",
        metric_key=metric_key,
    )

    assert metric_key == "power_w"
    assert [item["url"] for item in ranked] == ["fifty", "five", "one", "range"]
    assert ranked[0]["metrics"]["power_w"]["value"] == 50.0
    assert "power_w" not in ranked[-1]["metrics"]


def test_power_parser_distinguishes_megawatts_and_milliwatts():
    mega = ws._power_metric("100 MW", source="title")
    milli = ws._power_metric("100 mW", source="title")
    russian_mega = ws._power_metric("2 МВт", source="title")
    russian_milli = ws._power_metric("2 мВт", source="title")

    assert mega["value"] == 100_000_000.0
    assert milli["value"] == 0.1
    assert russian_mega["value"] == 2_000_000.0
    assert russian_milli["value"] == 0.002
    assert ws._power_metric("LaserJet P1820W", source="title") is None
    assert ws._power_metric("LaserJet M141w", source="title") is None


def test_data_rate_parser_distinguishes_bytes_bits_and_not_storage_capacity():
    items = [
        {"title": "Adapter 1 GB/s", "url": "bytes"},
        {"title": "Adapter 1 Gb/s", "url": "bits"},
    ]
    for item in items:
        ws._attach_catalog_metrics(item)

    assert items[0]["metrics"]["data_rate_mbps"]["value"] == 8000.0
    assert items[1]["metrics"]["data_rate_mbps"]["value"] == 1000.0
    assert "capacity_gb" not in items[0]["metrics"]
    assert "capacity_gb" not in items[1]["metrics"]


def test_non_price_result_does_not_claim_winner_without_comparable_metric():
    item = {
        "title": "Лазерная указка мощная",
        "url": "u",
        "in_stock": True,
        "price_value": 1000.0,
        "price_text": "1 000 ₽",
    }
    ws._attach_catalog_metrics(item)
    result = ws._shop_search_result(
        "лазер",
        "wildberries",
        ok=True,
        items=[item],
        best=None,
        criterion="power_desc",
        criterion_label="максимальная мощность",
        metric_key="",
    )

    assert result["comparison"]["complete"] is False
    assert result["comparison"]["compared_count"] == 0
    assert result["best"] is None


def test_non_price_result_requires_two_comparable_cards_for_superlative():
    item = {"title": "Лазер 5 W", "url": "u", "in_stock": True}
    ws._attach_catalog_metrics(item)
    result = ws._shop_search_result(
        "лазер",
        "wildberries",
        ok=True,
        items=[item],
        best=item,
        criterion="power_desc",
        metric_key="power_w",
    )

    assert result["comparison"]["compared_count"] == 1
    assert result["comparison"]["complete"] is False
    assert result["comparison"]["best_metric"] is None
    assert result["best"] is None


def test_catalog_constraints_are_normalized_filtered_and_returned():
    items = [
        {"title": "a", "price_value": 1000.0, "rating_value": 4.8},
        {"title": "b", "price_value": 4000.0, "rating_value": 4.9},
        {"title": "c", "price_value": None, "rating_value": None},
    ]
    constraints = ws._normalize_catalog_constraints(
        {"max_price": 3000, "min_rating": 4.5, "ignored": 1}
    )

    assert constraints == {"max_price": 3000.0, "min_rating": 4.5}
    assert ws._filter_catalog_constraints(items, constraints) == [items[0]]
    result = ws._shop_search_result(
        "x",
        "ozon",
        ok=True,
        items=[items[0]],
        constraints=constraints,
    )
    assert result["constraints"] == constraints
    assert ws._normalize_catalog_constraints({"min_price": 5, "max_price": 1}) == {}
    assert ws._normalize_catalog_constraints({"min_rating": 5.1}) == {}


def test_wildberries_api_parser_preserves_price_stock_and_rating():
    payload = {
        "products": [
            {
                "id": 721395131,
                "brand": "Pointer",
                "name": "Лазерная указка 50 000 мВт",
                "reviewRating": 4.8,
                "feedbacks": 50,
                "totalQuantity": 4,
                "sizes": [{"price": {"product": 302600}, "stocks": [{"qty": 4}]}],
            }
        ]
    }

    [item] = ws._wildberries_api_items(payload)

    assert item["price_value"] == 3026.0
    assert item["price_text"] == "3 026 ₽"
    assert item["in_stock"] is True
    assert item["rating_value"] == 4.8


def test_wildberries_api_prefers_regular_product_price_over_wallet_discount():
    payload = {
        "products": [
            {
                "id": 1,
                "name": "Товар",
                "sizes": [
                    {
                        "price": {"wallet": 90000, "product": 100000, "basic": 120000},
                        "stocks": [{"qty": 1}],
                    }
                ],
            }
        ]
    }

    [item] = ws._wildberries_api_items(payload)

    assert item["price_value"] == 1000.0


def test_catalog_search_uses_neutral_query_plus_optional_recall_variant():
    assert ws._catalog_search_query("лазер", "power_desc") == "лазер"
    assert ws._catalog_search_variants("лазер", "power_desc") == [
        "мощный лазер",
        "лазер",
    ]


def test_rank_catalog_items_cheapest_first_and_unpriced_last():
    items = [
        {"title": "b", "url": "u2", "price_value": 500.0, "price_text": "500 ₽"},
        {"title": "a", "url": "u1", "price_value": 100.0, "price_text": "100 ₽"},
        {"title": "no price", "url": "u3", "price_value": None, "price_text": ""},
    ]
    ranked = ws._rank_catalog_items(items)
    assert [item["url"] for item in ranked] == ["u1", "u2", "u3"]


def test_rank_catalog_items_prefers_purchasable_product_over_cheaper_analog():
    html = """
    <div class="catalog-product">
      <a class="catalog-product__name" href="/product/analog/a/rtx-5090/">RTX 5090 A</a>
      <div class="product-buy__price">409 999 ₽</div><a>Аналоги</a>
    </div>
    <div class="catalog-product">
      <a class="catalog-product__name" href="/product/b/rtx-5090-oc/">RTX 5090 B</a>
      <div class="product-buy__price">413 999 ₽</div><button>Купить</button>
    </div>
    """
    items = ws._extract_catalog_items(html, base_url="https://www.dns-shop.ru/search/")
    ranked = ws._rank_catalog_items(items)
    assert ranked[0]["title"] == "RTX 5090 B"
    assert ranked[0]["in_stock"] is True
    assert ranked[1]["in_stock"] is False


def test_non_price_ranking_prioritizes_metric_before_unknown_stock():
    items = [
        {"title": "5 W", "url": "five", "in_stock": True},
        {"title": "100 W", "url": "hundred", "in_stock": None},
    ]
    for item in items:
        ws._attach_catalog_metrics(item)

    ranked = ws._rank_catalog_items(
        items,
        criterion="power_desc",
        metric_key="power_w",
    )

    assert [item["url"] for item in ranked] == ["hundred", "five"]


def test_rating_ranking_accounts_for_review_volume():
    items = [
        {"title": "5 stars", "url": "tiny", "rating_value": 5.0, "review_count": 1},
        {
            "title": "4.9 stars",
            "url": "proven",
            "rating_value": 4.9,
            "review_count": 10_000,
        },
    ]
    for item in items:
        ws._attach_catalog_metrics(item)
    ranked = ws._rank_catalog_items(
        items,
        criterion="rating_desc",
        metric_key="rating_score",
    )

    assert ranked[0]["url"] == "proven"


def test_catalog_stock_does_not_treat_not_in_stock_as_positive_substring():
    html = """
    <div class="product-card">
      <a href="/product/a/item/">RTX 5090 currently not in stock</a>
      <div>409 999 ₽</div>
    </div>
    """
    [item] = ws._extract_catalog_items(html, base_url="https://shop.example/")
    assert item["in_stock"] is False


def test_catalog_query_filter_drops_cheaper_neighbour_models_and_category_links():
    items = [
        {
            "title": "Видеокарта RTX 5060 Dual",
            "url": "https://www.dns-shop.ru/product/5060/",
            "price_value": 33999.0,
        },
        {
            "title": "Видеокарты",
            "url": "https://www.dns-shop.ru/catalog/video/",
            "price_value": 409999.0,
        },
        {
            "title": "Видеокарта Palit GeForce RTX 5090 GameRock",
            "url": "https://www.dns-shop.ru/product/5090/",
            "price_value": 409999.0,
        },
    ]
    matched = ws._filter_catalog_items_for_query(items, "5090")
    assert [item["url"] for item in matched] == [
        "https://www.dns-shop.ru/product/5090/"
    ]


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("rtx5090", "Видеокарта RTX 5090"),
        ("2TB SSD", "Накопитель SSD 2 ТБ"),
        ("iphone16", "Смартфон Apple iPhone 16"),
    ],
)
def test_catalog_query_filter_matches_joined_and_split_model_tokens(query, title):
    item = {"title": title, "url": "https://shop.example/product/1", "price_value": 1.0}
    assert ws._filter_catalog_items_for_query([item], query) == [item]


def test_catalog_query_filter_keeps_requested_brand_strict():
    items = [
        {"title": "MSI GeForce RTX 5090", "url": "msi", "price_value": 1.0},
        {"title": "Palit GeForce RTX 5090", "url": "palit", "price_value": 2.0},
    ]
    matched = ws._filter_catalog_items_for_query(items, "Palit GeForce RTX 5090")
    assert [item["url"] for item in matched] == ["palit"]


@pytest.mark.parametrize("qualifier", ["Ti", "OC", "Pro", "Max"])
def test_catalog_query_filter_keeps_requested_model_qualifier_strict(qualifier):
    items = [
        {"title": "GeForce RTX 5090", "url": "base", "price_value": 1.0},
        {"title": f"GeForce RTX 5090 {qualifier}", "url": "qualified", "price_value": 2.0},
    ]
    matched = ws._filter_catalog_items_for_query(items, f"RTX 5090 {qualifier}")
    assert [item["url"] for item in matched] == ["qualified"]


def test_catalog_query_filter_ignores_inflected_product_category_word():
    item = {
        "title": "Видеокарта Palit GeForce RTX 5090",
        "url": "palit",
        "price_value": 1.0,
    }
    assert ws._filter_catalog_items_for_query([item], "видеокарту rtx 5090") == [item]


def test_dns_shop_search_uses_stable_chrome_without_known_bad_headless_probe(monkeypatch):
    surfer = ws.JarvisWebSurfer(
        ws.SurferConfig(headless=True, shopping_budget_sec=10, headful_shop_fallback=True)
    )
    surfer._playwright = object()
    calls: list[str] = []

    async def blocked(*_args, **_kwargs):
        calls.append("headless")
        return ws._shop_search_result(
            "5090", "dns", ok=False, error="HTTP 401 HTTP 403: no matching products parsed"
        )

    async def stable(*_args, **_kwargs):
        calls.append("stable")
        item = {
            "title": "RTX 5090",
            "url": "https://www.dns-shop.ru/product/5090/",
            "price_text": "409 999 ₽",
            "price_value": 409999.0,
        }
        return ws._shop_search_result(
            "5090",
            "dns",
            ok=True,
            items=[item],
            cheapest=item,
            browser_mode="headful_stable_chrome",
        )

    monkeypatch.setattr(ws.sys, "platform", "win32")
    monkeypatch.setattr(surfer, "_shop_search_impl", blocked)
    monkeypatch.setattr(surfer, "_shop_search_headful_chrome", stable)
    result = asyncio.run(surfer.shop_search("5090", shop="dns"))
    assert calls == ["stable"]
    assert result["ok"] is True
    assert result["cheapest"]["price_value"] == 409999.0
    assert result["browser_mode"] == "headful_stable_chrome"
    assert result["stages"][0]["name"] == "stable_chrome"
    assert result["stages"][0]["ok"] is True


def test_dns_stable_navigation_failure_is_not_reported_as_browser_unavailable(monkeypatch):
    surfer = ws.JarvisWebSurfer(
        ws.SurferConfig(headless=True, shopping_budget_sec=1, headful_shop_fallback=True)
    )
    surfer._playwright = object()

    async def headless_must_not_run(*_args, **_kwargs):
        raise AssertionError("DNS must skip the known-bad headless probe on Windows")

    async def navigation_failure(*_args, **_kwargs):
        raise ws.NavigationError("Navigation timed out: https://www.dns-shop.ru/search/")

    monkeypatch.setattr(ws.sys, "platform", "win32")
    monkeypatch.setattr(surfer, "_shop_search_impl", headless_must_not_run)
    monkeypatch.setattr(surfer, "_shop_search_headful_chrome", navigation_failure)

    result = asyncio.run(surfer.shop_search("5090", shop="dns"))

    assert result["ok"] is False
    assert result["error_code"] == "stable_navigation"
    assert "navigation failed" in result["error"]
    assert "unavailable" not in result["error"]


def test_shop_browser_context_loads_and_persists_per_shop_storage_state(tmp_path):
    state_dir = tmp_path / "shop-state"
    state_path = state_dir / "dns.json"
    state_dir.mkdir()
    state_path.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    captured: dict[str, object] = {}

    class Context:
        def set_default_timeout(self, value):
            captured["default_timeout"] = value

        def set_default_navigation_timeout(self, value):
            captured["nav_timeout"] = value

        async def storage_state(self, *, path):
            captured["saved_path"] = path

    class Browser:
        async def new_context(self, **kwargs):
            captured["kwargs"] = kwargs
            return Context()

    surfer = ws.JarvisWebSurfer(
        ws.SurferConfig(
            shop_storage_state_dir=str(state_dir),
            shop_persistent_profile_dir=str(tmp_path / "shop-profile"),
        )
    )

    async def exercise():
        context = await surfer._new_context(
            browser=Browser(),
            storage_state_path=surfer._shop_storage_state_path("dns"),
        )
        await surfer._persist_shop_storage_state(context, "dns")

    asyncio.run(exercise())

    assert captured["kwargs"]["storage_state"] == str(state_path)
    assert captured["saved_path"] == str(state_path)
    assert surfer._shop_persistent_profile_path("dns") == tmp_path / "shop-profile" / "dns"


def test_dns_persistent_profile_keeps_proxy_and_falls_back_on_slow_close(
    monkeypatch,
    tmp_path,
):
    captured: dict[str, object] = {}

    class FallbackBrowser:
        async def close(self):
            captured["fallback_browser_closed"] = True

    class Context:
        browser = FallbackBrowser()
        pages = [object()]

        def set_default_timeout(self, value):
            captured["default_timeout"] = value

        def set_default_navigation_timeout(self, value):
            captured["nav_timeout"] = value

        async def new_page(self):
            return object()

        async def storage_state(self, *, path):
            captured["saved_path"] = path

        async def close(self):
            captured["context_close_started"] = True
            await asyncio.Event().wait()

    class Chromium:
        async def launch_persistent_context(self, path, **kwargs):
            captured["profile_path"] = path
            captured["launch_kwargs"] = kwargs
            return Context()

        async def launch(self, **_kwargs):
            raise AssertionError("DNS must use its persistent stable-Chrome profile")

    surfer = ws.JarvisWebSurfer(
        ws.SurferConfig(
            proxies=["http://user:pass@127.0.0.1:8080"],
            shop_storage_state_dir=str(tmp_path / "shop-state"),
            shop_persistent_profile_dir=str(tmp_path / "shop-profile"),
        )
    )
    surfer._playwright = types.SimpleNamespace(chromium=Chromium())

    async def parsed(*_args, **_kwargs):
        return {"ok": True, "items": [{"title": "Ryzen"}]}

    monkeypatch.setattr(surfer, "_shop_search_page", parsed)
    monkeypatch.setattr(ws, "_PERSISTENT_CONTEXT_CLEANUP_TIMEOUT_SEC", 0.01)

    result = asyncio.run(
        surfer._shop_search_headful_chrome(
            "ryzen 9",
            "dns",
            "https://www.dns-shop.ru/search/?q=ryzen+9",
            20,
            [],
            "price_nearest",
            "nearest price",
            "ryzen 9",
            {"target_price": 50000.0},
        )
    )

    assert result["ok"] is True
    assert captured["profile_path"] == str(tmp_path / "shop-profile" / "dns")
    assert captured["launch_kwargs"]["proxy"] == {
        "server": "http://127.0.0.1:8080",
        "username": "user",
        "password": "pass",
    }
    assert captured["context_close_started"] is True
    assert captured["fallback_browser_closed"] is True


def test_wildberries_api_candidates_survive_browser_enrichment_failure(monkeypatch):
    surfer = ws.JarvisWebSurfer(
        ws.SurferConfig(headless=True, shopping_budget_sec=10, headful_shop_fallback=False)
    )
    surfer._playwright = object()
    captured: dict[str, str] = {}
    item = {
        "title": "Лазерная указка без заявленной мощности",
        "url": "https://www.wildberries.ru/catalog/1/detail.aspx",
        "price_text": "1 000 ₽",
        "price_value": 1000.0,
        "in_stock": True,
    }

    async def api_result(**_kwargs):
        return ws._shop_search_result(
            "лазер",
            "wildberries",
            ok=True,
            items=[item],
            criterion="power_desc",
            metric_key="",
            browser_mode="wildberries_catalog_api",
        )

    async def browser_failure(*args, **_kwargs):
        captured["url"] = args[2]
        return ws._shop_search_result(
            "лазер",
            "wildberries",
            ok=False,
            error="anti-bot",
            criterion="power_desc",
        )

    monkeypatch.setattr(surfer, "_wildberries_api_shop_search", api_result)
    monkeypatch.setattr(surfer, "_shop_search_impl", browser_failure)

    result = asyncio.run(
        surfer.shop_search("лазер", shop="wildberries", criterion="power_desc")
    )

    assert result["ok"] is True
    assert result["items"] == [item]
    assert result["comparison"]["complete"] is False
    assert result["best"] is None
    assert result["browser_mode"] == "wildberries_catalog_api"
    assert parse_qs(urlparse(captured["url"]).query)["search"] == ["мощный лазер"]


def test_wildberries_retries_api_after_incomplete_browser_result(monkeypatch):
    surfer = ws.JarvisWebSurfer(
        ws.SurferConfig(headless=True, shopping_budget_sec=20, headful_shop_fallback=False)
    )
    surfer._playwright = object()
    api_calls = 0
    browser_item = {
        "title": "Лазер без мощности",
        "url": "https://www.wildberries.ru/catalog/1/detail.aspx",
        "price_value": 1000.0,
    }
    api_items = [
        {
            "title": "Лазер 100 W",
            "url": "https://www.wildberries.ru/catalog/2/detail.aspx",
            "metrics": {"power_w": {"value": 100.0, "text": "100 W", "unit": "W"}},
        },
        {
            "title": "Лазер 50 W",
            "url": "https://www.wildberries.ru/catalog/3/detail.aspx",
            "metrics": {"power_w": {"value": 50.0, "text": "50 W", "unit": "W"}},
        },
    ]

    async def flaky_api(**_kwargs):
        nonlocal api_calls
        api_calls += 1
        if api_calls == 1:
            raise ws.WebSurferError("temporary rate limit")
        return ws._shop_search_result(
            "лазер",
            "wildberries",
            ok=True,
            items=api_items,
            best=api_items[0],
            criterion="power_desc",
            metric_key="power_w",
            browser_mode="wildberries_catalog_api",
        )

    async def incomplete_browser(*_args, **_kwargs):
        return ws._shop_search_result(
            "лазер",
            "wildberries",
            ok=True,
            items=[browser_item],
            criterion="power_desc",
            metric_key="",
            browser_mode="headless_chromium",
        )

    monkeypatch.setattr(surfer, "_wildberries_api_shop_search", flaky_api)
    monkeypatch.setattr(surfer, "_shop_search_impl", incomplete_browser)

    result = asyncio.run(
        surfer.shop_search("лазер", shop="wildberries", criterion="power_desc")
    )

    assert api_calls == 2
    assert result["comparison"]["complete"] is True
    assert result["best"]["url"].endswith("/2/detail.aspx")
    assert result["browser_mode"] == "wildberries_catalog_api"


def test_shop_search_result_shape():
    ranked = ws._rank_catalog_items(
        ws._extract_catalog_items(_DNS_GRID, base_url="https://www.dns-shop.ru/search/?q=rtx")
    )
    priced = [item for item in ranked if item["price_value"] is not None]
    result = ws._shop_search_result(
        "rtx 5090", "днс", ok=True, url="u", city="Москва", items=ranked, cheapest=priced[0]
    )
    assert result["ok"] is True
    assert result["shop"] == "dns"
    assert result["city"] == "Москва"
    assert result["count"] == 3
    assert result["cheapest"]["price_value"] == 409999.0


def test_catalog_from_jsonld_itemlist():
    html = """
    <script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[
      {"item":{"name":"RTX 5090 A","url":"/p/a","offers":{"price":"410000"}}},
      {"item":{"name":"RTX 5090 B","url":"/p/b","offers":{"price":"399000"}}}
    ]}
    </script>
    """
    items = ws._extract_catalog_items(html, base_url="https://shop.ru/")
    ranked = ws._rank_catalog_items(items)
    assert len(items) == 2
    assert ranked[0]["price_value"] == 399000.0
    assert ranked[0]["url"] == "https://shop.ru/p/b"


def test_catalog_from_jsonld_ranks_in_stock_offer_before_cheaper_out_of_stock():
    html = """
    <script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[
      {"item":{"name":"RTX 5090 unavailable","url":"/product/analog/a/item/",
        "offers":{"price":"409000","availability":"https://schema.org/OutOfStock"}}},
      {"item":{"name":"RTX 5090 available","url":"/product/b/item/",
        "offers":{"price":"499000","availability":"https://schema.org/InStock"}}}
    ]}
    </script>
    """
    ranked = ws._rank_catalog_items(
        ws._extract_catalog_items(html, base_url="https://www.dns-shop.ru/")
    )
    assert ranked[0]["title"] == "RTX 5090 available"
    assert ranked[0]["in_stock"] is True
    assert ranked[1]["in_stock"] is False
    assert "/product/analog/" not in ranked[1]["url"]


def _registry(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ToolRegistry(settings, storage, LLMRouter(settings)), storage


def test_web_shop_search_tool_registered_safe(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    spec = tools.get("web.shop_search")
    assert spec is not None
    assert spec.danger_level == "safe"
    assert spec.category == "web"
    storage.close()


def test_web_shop_search_requires_query_and_shop(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    no_query = asyncio.run(tools.run("web.shop_search", {"shop": "dns"}))
    assert no_query.ok is False
    no_shop = asyncio.run(tools.run("web.shop_search", {"query": "rtx 5090"}))
    assert no_shop.ok is False
    assert "shop" in no_shop.summary.lower() or "search_url" in no_shop.summary.lower()
    storage.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "x", "shop": "ozon", "search_url": "https://wildberries.ru/search"},
        {
            "query": "x",
            "shop": "wildberries",
            "search_url": "https://wildberries.ru.evil.test/search",
        },
        {"query": "x", "search_url": "http://127.0.0.1/catalog"},
        {"query": "x", "search_url": "file:///etc/passwd"},
    ],
)
def test_web_shop_search_rejects_unregistered_or_mismatched_urls(
    monkeypatch,
    tmp_path,
    arguments,
):
    async def fake_validate(url):
        return url

    monkeypatch.setattr("jarvis_gpt.tools._validate_public_http_url_async", fake_validate)
    tools, storage = _registry(monkeypatch, tmp_path)

    result = asyncio.run(tools.run("web.shop_search", arguments))

    assert result.ok is False
    assert "registered domain" in result.summary
    storage.close()


def test_web_shop_search_degrades_without_browser(monkeypatch, tmp_path):
    # Force the lazy import to fail so we exercise the honest-degradation path
    # regardless of whether Playwright happens to be installed.
    monkeypatch.setitem(sys.modules, "jarvis_gpt.web_surfer", None)
    tools, storage = _registry(monkeypatch, tmp_path)
    result = asyncio.run(tools.run("web.shop_search", {"query": "rtx 5090", "shop": "днс"}))
    assert result.ok is False
    assert (result.data or {}).get("needs_install") is True
    assert "playwright" in result.summary.lower()
    storage.close()


def test_web_shop_search_forwards_non_price_criterion_without_guessing(
    monkeypatch,
    tmp_path,
):
    captured = {}

    async def fake_start(self):
        self._started = True

    async def fake_close(self):
        self._started = False

    async def fake_shop_search(self, query, **kwargs):
        captured.update({"query": query, **kwargs})
        item = {
            "title": "Пылесос без указанной мощности",
            "url": "https://www.wildberries.ru/catalog/1/detail.aspx",
            "price_text": "10 000 ₽",
            "price_value": 10000.0,
            "in_stock": True,
        }
        return ws._shop_search_result(
            query,
            kwargs["shop"],
            ok=True,
            items=[item],
            criterion=kwargs["criterion"],
            criterion_label=kwargs["criterion_label"],
            metric_key="",
        )

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", fake_start)
    monkeypatch.setattr(ws.JarvisWebSurfer, "close", fake_close)
    monkeypatch.setattr(ws.JarvisWebSurfer, "shop_search", fake_shop_search)
    tools, storage = _registry(monkeypatch, tmp_path)

    result = asyncio.run(
        tools.run(
            "web.shop_search",
            {
                "query": "пылесос",
                "shop": "wildberries",
                "criterion": "power_desc",
                "criterion_label": "максимальная мощность",
            },
        )
    )

    assert result.ok is True
    assert captured["criterion"] == "power_desc"
    assert captured["criterion_label"] == "максимальная мощность"
    assert "Нет сопоставимой характеристики" in result.summary
    assert result.data["best"] is None
    storage.close()


def _dns_result(query: str = "ryzen 9", *, price: float = 49_000.0):
    item = {
        "title": "AMD Ryzen 9",
        "url": "https://www.dns-shop.ru/product/test/amd-ryzen-9/",
        "price_text": f"{int(price):,} ₽".replace(",", " "),
        "price_value": price,
        "in_stock": True,
    }
    return ws._shop_search_result(
        query,
        "dns",
        ok=True,
        items=[item],
        cheapest=item,
        best=item,
        browser_mode="headful_stable_chrome",
        price_sort_confirmed=True,
        criterion="price_asc",
        metric_key="price_value",
    )


def test_price_nearest_ranks_by_distance_and_reports_target():
    items = [
        {"title": "low", "url": "low", "price_value": 32_000.0},
        {"title": "near", "url": "near", "price_value": 49_000.0},
        {"title": "high", "url": "high", "price_value": 55_000.0},
    ]

    ranked = ws._rank_catalog_items(
        items,
        criterion="price_nearest",
        metric_key="price_value",
        target_price=50_000.0,
    )
    result = ws._shop_search_result(
        "ryzen 9",
        "dns",
        ok=True,
        items=ranked,
        best=ranked[0],
        criterion="price_nearest",
        metric_key="price_value",
        constraints={"target_price": 50_000.0},
    )

    assert ranked[0]["title"] == "near"
    assert result["comparison"]["target_price"] == 50_000.0
    assert result["comparison"]["distance_to_target"] == 1_000.0


def test_web_shop_search_singleflight_populates_exact_fresh_cache(monkeypatch, tmp_path):
    calls = 0
    captured_state_dir = ""

    async def fake_start(self):
        self._started = True

    async def fake_close(self):
        self._started = False

    async def fake_shop_search(self, query, **_kwargs):
        nonlocal calls, captured_state_dir
        calls += 1
        captured_state_dir = self.config.shop_storage_state_dir
        await asyncio.sleep(0.02)
        return _dns_result(query)

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", fake_start)
    monkeypatch.setattr(ws.JarvisWebSurfer, "close", fake_close)
    monkeypatch.setattr(ws.JarvisWebSurfer, "shop_search", fake_shop_search)
    tools, storage = _registry(monkeypatch, tmp_path)

    async def run_two():
        arguments = {"query": "ryzen 9", "shop": "dns", "criterion": "price_asc"}
        return await asyncio.gather(
            tools.run("web.shop_search", arguments),
            tools.run("web.shop_search", arguments),
        )

    first, second = asyncio.run(run_two())

    assert first.ok is True and second.ok is True
    assert calls == 1
    assert {first.data["cache"]["status"], second.data["cache"]["status"]} == {
        "miss_stored",
        "fresh_hit",
    }
    assert captured_state_dir.startswith(str(tmp_path))
    assert captured_state_dir.endswith("shop-state")
    storage.close()


def test_web_shop_search_hydrates_fresh_cache_from_verified_tool_run(
    monkeypatch,
    tmp_path,
):
    arguments = {"query": "ryzen 9", "shop": "dns", "criterion": "price_asc"}
    tools, storage = _registry(monkeypatch, tmp_path)
    prior = storage.record_tool_run(
        tool="web.shop_search",
        ok=True,
        summary="verified DNS catalog",
        arguments=arguments,
        data=_dns_result(),
    )

    async def must_not_start(_self):
        raise AssertionError("fresh history cache must avoid browser startup")

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", must_not_start)
    result = asyncio.run(tools.run("web.shop_search", arguments))

    assert result.ok is True
    assert result.data["cache"]["status"] == "fresh_hit"
    assert result.data["cache"]["provenance"] == {
        "source": "tool_run_history_live_catalog",
        "tool_run_id": prior["id"],
        "verified_catalog_result": True,
    }
    storage.close()


def test_history_hydration_rejects_cached_run_and_preserves_original_live_timestamp(
    monkeypatch,
    tmp_path,
):
    arguments = {"query": "ryzen 9", "shop": "dns", "criterion": "price_asc"}
    tools, storage = _registry(monkeypatch, tmp_path)
    verified_at = (
        datetime.now(UTC)
        - timedelta(seconds=tools_module.WEB_SHOP_CACHE_FRESH_TTL_SEC + 30)
    ).isoformat(timespec="seconds")
    original_live = _dns_result()
    original_live["provenance"] = {
        "source": "live_catalog",
        "verified_at": verified_at,
    }
    original_live["cache"] = {
        "status": "miss_stored",
        "cached_at": verified_at,
        "provenance": {"source": "live_catalog"},
    }
    storage.record_tool_run(
        tool="web.shop_search",
        ok=True,
        summary="original live DNS catalog",
        arguments=arguments,
        data=original_live,
    )
    replayed_cache = _dns_result()
    replayed_cache["provenance"] = {
        "source": "verified_catalog_cache",
        "cached_at": verified_at,
    }
    replayed_cache["cache"] = {
        "status": "fresh_hit",
        "cached_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": {"source": "tool_run_history_live_catalog"},
    }
    storage.record_tool_run(
        tool="web.shop_search",
        ok=True,
        summary="cached replay must not become live",
        arguments=arguments,
        data=replayed_cache,
    )
    live_calls = 0

    async def fake_start(self):
        self._started = True

    async def fake_close(self):
        self._started = False

    async def live_failure(_self, query, **_kwargs):
        nonlocal live_calls
        live_calls += 1
        return {
            "ok": False,
            "query": query,
            "shop": "dns",
            "items": [],
            "error": "anti-bot",
            "error_code": "anti_bot",
        }

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", fake_start)
    monkeypatch.setattr(ws.JarvisWebSurfer, "close", fake_close)
    monkeypatch.setattr(ws.JarvisWebSurfer, "shop_search", live_failure)

    result = asyncio.run(tools.run("web.shop_search", arguments))

    assert live_calls == 1
    assert result.ok is True
    assert result.data["cache"]["status"] == "stale_on_live_failure"
    assert result.data["cache"]["cached_at"] == verified_at
    assert result.data["cache"]["provenance"]["source"] == (
        "tool_run_history_live_catalog"
    )
    storage.close()


def test_web_shop_search_returns_stale_verified_cache_after_live_failure(
    monkeypatch,
    tmp_path,
):
    mode = "success"

    async def fake_start(self):
        self._started = True

    async def fake_close(self):
        self._started = False

    async def fake_shop_search(_self, query, **_kwargs):
        if mode == "success":
            return _dns_result(query)
        return {
            "ok": False,
            "query": query,
            "shop": "dns",
            "items": [],
            "error": "stable Chrome navigation failed",
            "error_code": "stable_navigation",
            "timings": {"total_ms": 8000},
            "stages": [{"name": "stable_chrome", "ok": False}],
        }

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", fake_start)
    monkeypatch.setattr(ws.JarvisWebSurfer, "close", fake_close)
    monkeypatch.setattr(ws.JarvisWebSurfer, "shop_search", fake_shop_search)
    tools, storage = _registry(monkeypatch, tmp_path)
    arguments = {"query": "ryzen 9", "shop": "dns", "criterion": "price_asc"}

    async def exercise():
        nonlocal mode
        first = await tools.run("web.shop_search", arguments)
        records = storage.get_runtime_value(tools_module.WEB_SHOP_CACHE_KEY, [])
        records[0]["cached_at"] = (
            datetime.now(UTC)
            - timedelta(seconds=tools_module.WEB_SHOP_CACHE_FRESH_TTL_SEC + 5)
        ).isoformat(timespec="seconds")
        storage.set_runtime_value(tools_module.WEB_SHOP_CACHE_KEY, records)
        mode = "failure"
        second = await tools.run("web.shop_search", arguments)
        return first, second

    first, second = asyncio.run(exercise())

    assert first.ok is True
    assert second.ok is True
    assert second.data["items"] == first.data["items"]
    assert second.data["cache"]["status"] == "stale_on_live_failure"
    assert second.data["cache"]["live_failure"]["error_code"] == "stable_navigation"
    assert second.data["cache"]["cached_at"]
    storage.close()


def test_web_shop_search_singleflight_queue_wait_obeys_absolute_deadline(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(tools_module, "WEB_SHOP_LIVE_TIMEOUT_SEC", 0.05)
    tools, storage = _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(storage, "record_tool_run", lambda **_kwargs: {})
    monkeypatch.setattr(storage, "add_event", lambda **_kwargs: {})
    browser_started = False

    async def must_not_start(_self):
        nonlocal browser_started
        browser_started = True

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", must_not_start)

    async def exercise():
        lock = tools._shop_search_locks.setdefault("dns", asyncio.Lock())
        await lock.acquire()
        try:
            started = asyncio.get_running_loop().time()
            result = await tools.run(
                "web.shop_search",
                {"query": "queued", "shop": "dns"},
            )
            elapsed = asyncio.get_running_loop().time() - started
        finally:
            lock.release()
        return result, elapsed

    result, elapsed = asyncio.run(exercise())

    assert result.ok is False
    assert result.data["error_code"] == "queue_timeout"
    assert browser_started is False
    assert elapsed < 0.15
    storage.close()


def test_web_shop_search_does_not_start_live_browser_after_late_lock_acquire(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(tools_module, "WEB_SHOP_LIVE_TIMEOUT_SEC", 0.05)
    remaining = iter((0.05, 0.005))
    monkeypatch.setattr(
        tools_module,
        "_shop_deadline_remaining",
        lambda _deadline: next(remaining),
    )
    tools, storage = _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(storage, "record_tool_run", lambda **_kwargs: {})
    monkeypatch.setattr(storage, "add_event", lambda **_kwargs: {})
    browser_started = False

    async def must_not_start(_self):
        nonlocal browser_started
        browser_started = True

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", must_not_start)

    async def exercise():
        return await tools.run(
            "web.shop_search",
            {"query": "queued", "shop": "dns"},
        )

    result = asyncio.run(exercise())

    assert result.ok is False
    assert result.data["error_code"] == "queue_budget_exhausted"
    assert browser_started is False
    storage.close()


def test_web_shop_search_total_deadline_cancels_and_drains_live_call(monkeypatch, tmp_path):
    cancelled = asyncio.Event()

    async def fake_start(self):
        self._started = True

    async def fake_close(self):
        self._started = False

    async def hanging_shop_search(_self, _query, **_kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr(ws.JarvisWebSurfer, "start", fake_start)
    monkeypatch.setattr(ws.JarvisWebSurfer, "close", fake_close)
    monkeypatch.setattr(ws.JarvisWebSurfer, "shop_search", hanging_shop_search)
    monkeypatch.setattr(tools_module, "WEB_SHOP_LIVE_TIMEOUT_SEC", 0.05)
    tools, storage = _registry(monkeypatch, tmp_path)

    async def exercise():
        started = asyncio.get_running_loop().time()
        result = await tools.run(
            "web.shop_search",
            {"query": "never", "shop": "dns"},
        )
        return result, asyncio.get_running_loop().time() - started

    result, elapsed = asyncio.run(exercise())

    assert result.ok is False
    assert result.data["error_code"] == "total_timeout"
    assert cancelled.is_set()
    assert elapsed < 1.0
    storage.close()
