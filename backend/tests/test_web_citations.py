"""Academic-style reference formatting on web research citations."""

from __future__ import annotations

from jarvis_gpt.tools import _format_reference, _research_citations, _url_domain


def test_research_citations_include_site_and_reference():
    sources = [
        {
            "url": "https://arxiv.org/abs/2401.00001",
            "title": "A Study of Things",
            "quality": "primary-official",
            "evidence_id": "ev-1",
        },
        {"url": "https://blog.example.com/post", "title": "A Post"},
        {"title": "source without url is skipped"},
    ]
    citations = _research_citations(sources)
    assert len(citations) == 2

    first = citations[0]
    expected_site = _url_domain("https://arxiv.org/abs/2401.00001")
    assert first["id"] == "1"
    assert first["site"] == expected_site
    assert first["quality"] == "primary-official"
    assert first["evidence_id"] == "ev-1"
    assert first["reference"].startswith("[1] A Study of Things")
    assert expected_site in first["reference"]
    assert first["url"] in first["reference"]


def test_format_reference_omits_missing_site():
    assert _format_reference(3, "Title", "", "https://x.example/y") == (
        "[3] Title. https://x.example/y"
    )


def test_format_reference_orders_index_title_site_url():
    reference = _format_reference(2, "Deep Report", "nature.com", "https://nature.com/a")
    assert reference == "[2] Deep Report. nature.com. https://nature.com/a"
