"""Yandex Search API v2 provider (the AI Studio key).

The live free HTML scrapers (DuckDuckGo/Bing/Yandex/Mojeek) get anti-bot
challenged from a datacenter IP, so web search silently blanks. Yandex Search
API v2 is the official, keyed, captcha-free path: an ``Api-Key`` auth header, a
``folderId`` in the POST body, and a response whose results arrive as a
base64-encoded XML document in the JSON ``rawData`` field. These tests pin the
request builder, the base64->XML parser, and the key/folder gating.
"""

from __future__ import annotations

import base64
import json

from jarvis_gpt.tools import (
    _api_search_request,
    _available_api_search_providers,
    _parse_yandex_api_results,
    _search_api_readiness,
    _search_provider_auth_headers,
)

# Shaped like a real Yandex Search API XML payload: <doc> per result with
# <url>/<title>/<headline>/<passages>, and inline <hlword> highlight tags that
# must be flattened away.
_YANDEX_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0">
  <response>
    <results>
      <grouping>
        <group>
          <doc>
            <url>https://nodejs.org/en/about/previous-releases</url>
            <title>Node.js <hlword>Releases</hlword></title>
            <headline>Node.js release schedule and LTS versions.</headline>
          </doc>
        </group>
        <group>
          <doc>
            <url>https://example.com/lts</url>
            <title>What are <hlword>LTS</hlword> versions</title>
            <passages>
              <passage>A detailed <hlword>guide</hlword> to LTS versions.</passage>
            </passages>
          </doc>
        </group>
      </grouping>
    </results>
  </response>
</yandexsearch>"""


def _response_body(xml: str) -> str:
    raw = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return json.dumps({"rawData": raw})


def test_parse_yandex_api_decodes_base64_xml_docs():
    results = _parse_yandex_api_results(_response_body(_YANDEX_XML), limit=6, vertical="web")
    assert [r["url"] for r in results] == [
        "https://nodejs.org/en/about/previous-releases",
        "https://example.com/lts",
    ]
    # Highlight tags are flattened into the title/snippet text.
    assert results[0]["title"] == "Node.js Releases"
    assert results[0]["snippet"] == "Node.js release schedule and LTS versions."
    # Falls back to <passages> when there is no <headline>.
    assert results[1]["title"] == "What are LTS versions"
    assert "detailed guide to LTS" in results[1]["snippet"]
    assert results[0]["rank"] == 1 and results[1]["rank"] == 2


def test_parse_yandex_api_degrades_on_bad_input():
    assert _parse_yandex_api_results("not json", limit=5, vertical="web") == []
    assert _parse_yandex_api_results(json.dumps({}), limit=5, vertical="web") == []
    assert _parse_yandex_api_results(json.dumps({"rawData": "!!not-base64!!"}),
                                     limit=5, vertical="web") == []
    assert _parse_yandex_api_results(
        json.dumps({"rawData": base64.b64encode(b"<broken").decode()}),
        limit=5, vertical="web",
    ) == []


def test_yandex_api_request_shape(monkeypatch):
    monkeypatch.setenv("JARVIS_YANDEX_SEARCH_API_KEY", "AQVN-secret")
    monkeypatch.setenv("JARVIS_YANDEX_SEARCH_FOLDER_ID", "b1gtestfolder")
    request = _api_search_request(
        "yandex_api", "node lts", region="ru-ru", freshness="", vertical="web", limit=5
    )
    assert request["method"] == "POST"
    assert request["url"] == "https://searchapi.api.cloud.yandex.net/v2/web/search"
    assert request["missing_key"] is False
    assert request["json"]["folderId"] == "b1gtestfolder"
    assert request["json"]["responseFormat"] == "FORMAT_XML"
    assert request["json"]["query"]["queryText"] == "node lts"
    assert request["json"]["query"]["searchType"] == "SEARCH_TYPE_RU"
    # The key itself never leaks into the request body/url; it rides the auth header.
    assert "AQVN-secret" not in json.dumps(request)
    assert _search_provider_auth_headers("yandex_api") == {"Authorization": "Api-Key AQVN-secret"}


def test_yandex_api_region_switches_search_type(monkeypatch):
    monkeypatch.setenv("JARVIS_YANDEX_SEARCH_API_KEY", "k")
    monkeypatch.setenv("JARVIS_YANDEX_SEARCH_FOLDER_ID", "f")
    request = _api_search_request(
        "yandex_api", "q", region="en-us", freshness="", vertical="web", limit=5
    )
    assert request["json"]["query"]["searchType"] == "SEARCH_TYPE_COM"


def test_yandex_api_requires_both_key_and_folder(monkeypatch):
    monkeypatch.delenv("JARVIS_YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_YANDEX_SEARCH_FOLDER_ID", raising=False)
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_SEARCH_FOLDER_ID", raising=False)
    # Neither configured.
    assert "yandex_api" not in _available_api_search_providers("web")
    assert _api_search_request(
        "yandex_api", "q", region="ru-ru", freshness="", vertical="web", limit=5
    )["missing_key"] is True
    # Key without folder is still incomplete (folderId is mandatory in the body).
    monkeypatch.setenv("JARVIS_YANDEX_SEARCH_API_KEY", "k")
    assert "yandex_api" not in _available_api_search_providers("web")
    assert _api_search_request(
        "yandex_api", "q", region="ru-ru", freshness="", vertical="web", limit=5
    )["missing_key"] is True
    # Both present -> available for the web vertical, listed as configured.
    monkeypatch.setenv("JARVIS_YANDEX_SEARCH_FOLDER_ID", "f")
    assert _available_api_search_providers("web") == ["yandex_api"]
    assert "yandex_api" not in _available_api_search_providers("images")
    assert "yandex_api" in _search_api_readiness()["configured"]
