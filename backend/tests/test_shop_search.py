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


def test_extract_catalog_items_from_dns_like_grid():
    items = ws._extract_catalog_items(_DNS_GRID, base_url="https://www.dns-shop.ru/search/?q=rtx")
    # Three products; the nav "Помощь" link has no nearby price and is excluded.
    assert len(items) == 3
    urls = {item["url"] for item in items}
    assert all("/product/" in url for url in urls)
    assert all("dns-shop.ru" in url for url in urls)
    values = sorted(item["price_value"] for item in items)
    assert values == [409999.0, 413999.0, 499999.0]


def test_rank_catalog_items_cheapest_first_and_unpriced_last():
    items = [
        {"title": "b", "url": "u2", "price_value": 500.0, "price_text": "500 ₽"},
        {"title": "a", "url": "u1", "price_value": 100.0, "price_text": "100 ₽"},
        {"title": "no price", "url": "u3", "price_value": None, "price_text": ""},
    ]
    ranked = ws._rank_catalog_items(items)
    assert [item["url"] for item in ranked] == ["u1", "u2", "u3"]


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
