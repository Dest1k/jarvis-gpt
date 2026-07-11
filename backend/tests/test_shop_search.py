"""Coverage for the browser shop-search: catalog parse, price ranking, tool wiring."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
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


def test_shop_search_retries_blocked_headless_catalog_in_stable_chrome(monkeypatch):
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
    assert calls == ["headless", "stable"]
    assert result["ok"] is True
    assert result["cheapest"]["price_value"] == 409999.0
    assert result["browser_mode"] == "headful_stable_chrome"


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
