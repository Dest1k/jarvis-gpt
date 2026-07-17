"""Full-text archive search intent detection (doc area 2).

"в каком документе упоминается X?" runs a corpus-wide FTS sweep (files.search) and
answers with the matching documents + quoted passages, rather than recalling one
document by identity. These tests pin the term extraction that gates that route.
"""

from __future__ import annotations

from jarvis_gpt.agent import _archive_search_term, _document_compare_focus


def test_extracts_term_from_russian_archive_queries():
    assert _archive_search_term("в каком документе упоминается Нестеренко?") == "Нестеренко"
    assert _archive_search_term("в каких файлах есть бюджет отдела") == "бюджет отдела"
    assert _archive_search_term("найди в документах слово зарплата") == "зарплата"
    assert _archive_search_term("поищи в файлах Скипин") == "Скипин"


def test_extracts_term_from_english_archive_queries():
    assert _archive_search_term("which document mentions revenue") == "revenue"
    assert _archive_search_term("search my files for onboarding") == "onboarding"


def test_does_not_fire_for_recall_or_generation():
    # A recall by identity is not an archive FTS sweep.
    assert _archive_search_term("найди договор alpha") is None
    assert _archive_search_term("достань из памяти отчёт за квартал") is None
    # Generation is not a search.
    assert _archive_search_term("создай документ про бюджет") is None
    assert _archive_search_term("какая погода сегодня") is None


def test_term_is_bounded_and_trimmed():
    term = _archive_search_term(
        "в каком документе упоминается очень длинная фраза из многих разных слов подряд ещё"
    )
    assert term is not None and len(term.split()) <= 6
    assert _archive_search_term("в каком файле есть НДС!") == "НДС"  # trailing punct stripped


def test_compare_focus_detection():
    # A comparison request yields a cross-document instruction; a plain summary does not.
    assert _document_compare_focus("сравни документы за 15 июля") is not None
    assert _document_compare_focus("найди противоречия между отчётами") is not None
    assert _document_compare_focus("чем отличаются эти документы") is not None
    assert _document_compare_focus("compare these files") is not None
    assert _document_compare_focus("сделай вывод из документов за вчера") is None
    assert _document_compare_focus("какие документы были 16 июля") is None
