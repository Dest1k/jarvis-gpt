from jarvis_gpt.shop_registry import (
    find_shop_source,
    find_shop_sources,
    get_shop_source,
    shop_search_url,
)


def test_shop_registry_recognizes_wildberries_spellings_and_short_aliases():
    for text in (
        "на Wildberries",
        "на вайлдберриз",
        "на вайлдберрис",
        "на вайлдбериз",
        "на вб",
        "on wb",
    ):
        assert find_shop_source(text).key == "wildberries"


def test_shop_registry_recognizes_other_localized_marketplaces():
    assert find_shop_source("на озон").key == "ozon"
    assert find_shop_source("в ДНС").key == "dns"
    assert find_shop_source("на Яндекс.Маркете").key == "yandex market"
    assert find_shop_source("в м.видео").key == "mvideo"
    assert find_shop_source("на озоне").key == "ozon"
    assert find_shop_source("в Ситилинке").key == "citilink"
    assert find_shop_source("в Регарде").key == "regard"
    assert find_shop_source("на Авито").key == "avito"
    assert find_shop_source("на Алиэкспресс").key == "aliexpress"


def test_shop_registry_uses_token_boundaries_and_builds_search_urls():
    assert find_shop_source("newbie") is None
    assert get_shop_source("wildberries.ru").key == "wildberries"
    assert get_shop_source("www.wildberries.ru").key == "wildberries"
    assert "search=" in shop_search_url("вайлдберрис", "лазер мощный")


def test_shop_registry_returns_multiple_sources_in_textual_order():
    assert [source.key for source in find_shop_sources("Wildberries или Ozon")] == [
        "wildberries",
        "ozon",
    ]
    assert [source.key for source in find_shop_sources("Ozon или Wildberries")] == [
        "ozon",
        "wildberries",
    ]
