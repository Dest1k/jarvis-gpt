"""Follow-up requests must inherit the search subject from recent conversation.

A request like "найди ссылки на dns" or "а в рублях?" carries no product of its own;
the shop/web search must recover the subject (e.g. "5070 5090") from prior turns instead
of searching the store for the filler words.
"""

from __future__ import annotations

from jarvis_gpt.agent import _pick_subject_from_messages, _subject_is_vague


def test_subject_is_vague_detects_followup_filler():
    assert _subject_is_vague("конкретные ссылки например тебе удобно") is True
    assert _subject_is_vague("найди ссылки на dns") is False  # "dns" is a latin token
    assert _subject_is_vague("где купить") is True
    assert _subject_is_vague("а в рублях") is True  # currency qualifier only
    assert _subject_is_vague("") is True


def test_subject_is_vague_keeps_real_subjects():
    assert _subject_is_vague("5070 и 5090") is False
    assert _subject_is_vague("RTX 5090") is False
    assert _subject_is_vague("стиральная машина") is False  # concrete cyrillic noun
    assert _subject_is_vague("поездка в екатеринбург") is False


def test_pick_subject_recovers_product_across_followups():
    messages = [
        {"role": "user", "content": "сравни цены на 5070 и 5090"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "а в рублях?"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "найди конкретные ссылки на dns"},
    ]
    got = _pick_subject_from_messages("найди конкретные ссылки на dns", messages)
    assert got is not None
    assert "5070" in got and "5090" in got  # the currency follow-up did not hijack it


def test_pick_subject_prefers_product_like_over_incidental_noun():
    messages = [
        {"role": "user", "content": "нужна видеокарта RTX 4090"},
        {"role": "user", "content": "а в долларах сколько"},
        {"role": "user", "content": "дай ссылки"},
    ]
    got = _pick_subject_from_messages("дай ссылки", messages)
    assert got is not None and "4090" in got


def test_pick_subject_none_without_prior_subject():
    messages = [{"role": "user", "content": "найди ссылки где удобно"}]
    assert _pick_subject_from_messages("найди ссылки где удобно", messages) is None
