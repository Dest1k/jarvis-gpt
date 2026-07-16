"""Regression: web_surfer HTML→Markdown extraction keeps data, not page chrome.

The deep_research crawler used to emit every ``<a>`` as a Markdown link and skipped
tables, so a currency page came back as hundreds of navigation/converter links with
zero actual rates. The cleaner now drops link-farms, extracts table cells, and keeps
a plain-text fallback so the output is never empty and never a URL dump.
"""

from __future__ import annotations

from jarvis_gpt.web_surfer import JarvisWebSurfer, _compose_research_report


def _clean(html: str) -> str:
    return JarvisWebSurfer()._clean_html_to_markdown(html, base_url="https://example.ru")


def test_cleaner_keeps_table_data_and_drops_link_farms():
    nav_links = "".join(f'<a href="/c/{i}">Валюта{i}</a>' for i in range(20))
    html = f"""<html><body>
      <nav><a href="/">Главная</a><a href="/news">Новости</a></nav>
      <div class="cross-rates">{nav_links}</div>
      <article>
        <h1>Курсы валют ЦБ РФ на сегодня</h1>
        <table>
          <tr><th>Валюта</th><th>Курс ЦБ</th></tr>
          <tr><td>Доллар США</td><td>78.50 руб.</td></tr>
          <tr><td>Евро</td><td>91.20 руб.</td></tr>
        </table>
        <p>ЦБ РФ установил официальные курсы валют на 16 июля 2026 года.</p>
      </article>
      <footer><a href="/about">О компании</a></footer>
    </body></html>"""
    md = _clean(html)
    # Real data survives.
    assert "78.50" in md and "91.20" in md
    assert "Доллар США" in md
    assert "официальные курсы" in md
    # Navigation and cross-rate link-farms are gone.
    assert "Главная" not in md
    assert "Валюта5" not in md
    # Not a giant dump.
    assert len(md) < 1000


def test_cleaner_never_returns_empty_for_textful_page():
    # Content in a non-block element still yields text via the fallback.
    html = "<html><body><section><span>Важный факт: ответ 42.</span></section></body></html>"
    md = _clean(html)
    assert "42" in md


def test_cleaner_does_not_emit_standalone_link_urls():
    html = (
        "<html><body><article><p>Смотри "
        '<a href="https://example.com/page">эту страницу</a> подробнее.</p>'
        "</article></body></html>"
    )
    md = _clean(html)
    assert "эту страницу" in md
    # The inline URL is not dumped as a standalone markdown link line.
    assert "https://example.com/page" not in md


def test_research_report_falls_back_to_snippets_when_pages_unreadable():
    snippets = [
        {"title": "Курс ЦБ РФ", "url": "https://cbr.ru", "snippet": "Доллар США — 78.50 руб."},
        {"title": "Курсы валют", "url": "https://bankiros.ru", "snippet": "Евро — 91.20 руб."},
    ]
    report = _compose_research_report("курс валют", "", [], snippets)
    assert "78.50" in report and "91.20" in report
    assert "выдержки из результатов поиска" in report
    assert "https://cbr.ru" in report


def test_research_report_honest_when_no_sections_and_no_snippets():
    report = _compose_research_report("что-то", "", [], [])
    assert "не дали читаемого текста" in report
