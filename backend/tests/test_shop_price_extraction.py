"""schema.org JSON-LD product/price extraction (task #5, increment 1).

SSR stores (Яндекс Маркет, Regard, and many schema.org e-commerce sites) emit
product offers in <script type="application/ld+json"> blocks, so real prices come
out of a plain fetch — no fragile per-store CSS selectors, no Playwright. Anti-bot
SPAs (DNS 401 / Ozon) return no HTML and are a later increment. Fixtures below mirror
the real structures observed live: Яндекс Маркет ItemList→Product→offers.price and
Regard OfferCatalog→Offer.
"""

from __future__ import annotations

from jarvis_gpt.tools import (
    _format_price_value,
    _jsonld_offer_price,
    _jsonld_products_from_html,
    _shop_query_focus,
)

_YANDEX_MARKET = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebSite","url":"https://market.yandex.ru"}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","name":"Rtx 5090",
 "itemListElement":[
  {"@type":"ListItem","position":1,"item":{"@type":"Product",
    "name":"Видеокарта MSI GeForce RTX 5090 Gaming Trio OC 32 GB",
    "url":"https://market.yandex.ru/card/x/5303117087",
    "offers":{"@type":"Offer","availability":"https://schema.org/InStock",
      "price":539740,"priceCurrency":"RUB"}}},
  {"@type":"ListItem","position":2,"item":{"@type":"Product",
    "name":"Видеокарта nVidia GeForce RTX 5090 32Gb",
    "url":"https://market.yandex.ru/card/x/5466938094",
    "offers":{"@type":"Offer","price":477712,"priceCurrency":"RUB"}}}]}
</script>
"""

_REGARD = """
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"OfferCatalog","name":"Rtx 5090",
 "itemListElement":[
  {"@type":"Offer","name":"Видеокарта NVIDIA GeForce RTX 5090 Palit GameRock OC 32GB",
   "url":"https://www.regard.ru/product/732634/x","price":419990,
   "priceCurrency":"RUB","availability":"https://schema.org/InStock"}]}
</script>
"""


def test_extracts_products_from_yandex_market_itemlist():
    products = _jsonld_products_from_html(_YANDEX_MARKET)
    assert len(products) == 2
    first = products[0]
    assert "MSI GeForce RTX 5090" in first["name"]
    assert first["price"] == "539 740 ₽"
    assert first["price_value"] == 539740
    assert first["currency"] == "RUB"
    assert first["url"] == "https://market.yandex.ru/card/x/5303117087"
    assert first["in_stock"] is True
    # Second product has no availability -> not marked in stock, price still parsed.
    assert products[1]["price"] == "477 712 ₽"
    assert products[1]["in_stock"] is False


def test_extracts_offer_from_regard_offercatalog():
    products = _jsonld_products_from_html(_REGARD)
    assert len(products) == 1
    assert products[0]["price"] == "419 990 ₽"
    assert "Palit GameRock" in products[0]["name"]


def test_ignores_non_product_jsonld_and_bad_blocks():
    assert _jsonld_products_from_html("") == []
    assert _jsonld_products_from_html("<script type='application/ld+json'>{oops</script>") == []
    org = (
        "<script type=\"application/ld+json\">"
        '{"@type":"Organization","name":"Регард"}</script>'
    )
    assert _jsonld_products_from_html(org) == []


def test_dedups_and_respects_limit():
    block = (
        '<script type="application/ld+json">{"@type":"Product","name":"RTX 5090",'
        '"offers":{"price":100,"priceCurrency":"RUB"}}</script>'
    )
    # Same product twice -> one row.
    assert len(_jsonld_products_from_html(block + block)) == 1


def test_offer_price_prefers_cheapest_and_reads_aggregate():
    price, cur = _jsonld_offer_price(
        {"offers": [{"price": 500, "priceCurrency": "RUB"}, {"price": 420}]}
    )
    assert price == 420.0 and cur == "RUB"
    agg, _ = _jsonld_offer_price({"offers": {"@type": "AggregateOffer", "lowPrice": 399990}})
    assert agg == 399990.0


def test_price_formatting_by_currency():
    assert _format_price_value(539740, "RUB") == "539 740 ₽"
    assert _format_price_value(1999, "USD") == "$1 999"
    assert _format_price_value(1200, "EUR") == "1 200 €"


def test_shop_query_focus_strips_filler_and_finds_model_anchor():
    # The messy orchestrator query keeps the product, drops command/filler words, and
    # anchors on the model number so a store's "you might also like" items get filtered.
    text, anchors = _shop_query_focus(
        "где сейчас дешевле всего купить видеокарту RTX 5090? сравни несколько магазинов"
    )
    assert "rtx" in text and "5090" in text
    assert "купить" not in text and "дешевле" not in text and "магазинов" not in text
    assert anchors == ["5090"]


def test_shop_query_focus_anchor_falls_back_to_brand_token():
    _text, anchors = _shop_query_focus("холодильник Bosch недорого")
    assert anchors == ["bosch"]  # no digit token -> distinctive latin brand token


def test_shop_query_focus_no_anchor_for_generic_category():
    # A bare category has nothing distinctive to filter on -> no targeted store fetch.
    _text, anchors = _shop_query_focus("купить видеокарту")
    assert anchors == []
