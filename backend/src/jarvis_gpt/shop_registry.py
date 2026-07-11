"""Canonical marketplace aliases and search endpoints shared by Jarvis routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus


@dataclass(frozen=True, slots=True)
class ShopSource:
    key: str
    domain: str
    search_url_template: str
    aliases: tuple[str, ...]

    def search_url(self, query: str) -> str:
        return self.search_url_template.format(q=quote_plus(query))


SHOP_SOURCES: tuple[ShopSource, ...] = (
    ShopSource(
        key="dns",
        domain="dns-shop.ru",
        search_url_template="https://www.dns-shop.ru/search/?q={q}",
        aliases=(r"dns(?:-shop)?(?:\.ru)?", r"днс"),
    ),
    ShopSource(
        key="ozon",
        domain="ozon.ru",
        search_url_template="https://www.ozon.ru/search/?text={q}",
        aliases=(r"ozon", r"озон(?:е|а)?"),
    ),
    ShopSource(
        key="wildberries",
        domain="wildberries.ru",
        search_url_template="https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
        aliases=(r"wildberr(?:y|ies)", r"ва[йи]лдбер[а-яё]*", r"вб", r"wb"),
    ),
    ShopSource(
        key="yandex market",
        domain="market.yandex.ru",
        search_url_template="https://market.yandex.ru/search?text={q}",
        aliases=(
            r"яндекс[.\s-]*маркет(?:е|а)?",
            r"yandex[.\s-]*market",
            r"(?:на|в)\s+маркет(?:е|а)?",
        ),
    ),
    ShopSource(
        key="citilink",
        domain="citilink.ru",
        search_url_template="https://www.citilink.ru/search/?text={q}",
        aliases=(r"citilink", r"ситилинк(?:е|а)?"),
    ),
    ShopSource(
        key="mvideo",
        domain="mvideo.ru",
        search_url_template="https://www.mvideo.ru/product-list-page?q={q}",
        aliases=(r"m[.\s-]*video", r"м[.\s-]*видео", r"мвидео"),
    ),
    ShopSource(
        key="eldorado",
        domain="eldorado.ru",
        search_url_template="https://www.eldorado.ru/search/catalog.php?q={q}",
        aliases=(r"eldorado", r"эльдорадо"),
    ),
    ShopSource(
        key="regard",
        domain="regard.ru",
        search_url_template="https://www.regard.ru/catalog?search={q}",
        aliases=(r"regard", r"регард(?:е|а)?"),
    ),
    ShopSource(
        key="avito",
        domain="avito.ru",
        search_url_template="https://www.avito.ru/rossiya?q={q}",
        aliases=(r"avito", r"авито"),
    ),
    ShopSource(
        key="aliexpress",
        domain="aliexpress.ru",
        search_url_template="https://aliexpress.ru/wholesale?SearchText={q}",
        aliases=(r"aliexpress", r"ali[.\s-]*express", r"али[а-яё]*экспресс"),
    ),
)

_BY_KEY = {source.key: source for source in SHOP_SOURCES}
_BY_DOMAIN = {source.domain: source for source in SHOP_SOURCES}
_TOKEN_LEFT = r"(?<![a-zа-яё0-9])"
_TOKEN_RIGHT = r"(?![a-zа-яё0-9])"


def normalize_shop_text(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def find_shop_source(text: str) -> ShopSource | None:
    normalized = normalize_shop_text(text)
    for source in SHOP_SOURCES:
        for alias in source.aliases:
            if re.search(f"{_TOKEN_LEFT}(?:{alias}){_TOKEN_RIGHT}", normalized):
                return source
    return None


def find_shop_sources(text: str) -> tuple[ShopSource, ...]:
    """Return every explicitly mentioned source in textual order."""

    normalized = normalize_shop_text(text)
    found: list[tuple[int, ShopSource]] = []
    for source in SHOP_SOURCES:
        starts = [
            match.start()
            for alias in source.aliases
            if (match := re.search(f"{_TOKEN_LEFT}(?:{alias}){_TOKEN_RIGHT}", normalized))
        ]
        if starts:
            found.append((min(starts), source))
    return tuple(source for _position, source in sorted(found, key=lambda item: item[0]))


def get_shop_source(value: str | None) -> ShopSource | None:
    normalized = normalize_shop_text(value or "")
    if normalized in _BY_KEY:
        return _BY_KEY[normalized]
    if normalized in _BY_DOMAIN:
        return _BY_DOMAIN[normalized]
    host_source = get_shop_source_by_host(normalized)
    if host_source is not None:
        return host_source
    return find_shop_source(normalized)


def get_shop_source_by_host(value: str | None) -> ShopSource | None:
    """Resolve a hostname without applying loose human-language aliases."""

    hostname = normalize_shop_text(value or "").removeprefix("www.").rstrip(".")
    for domain, source in _BY_DOMAIN.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return source
    return None


def shop_search_url(shop: str | None, query: str) -> str:
    source = get_shop_source(shop)
    return source.search_url(query) if source else ""
