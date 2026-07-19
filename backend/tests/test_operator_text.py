"""Operator text normalization: layout flip, smash scrub, fuzzy match."""

from __future__ import annotations

from jarvis_gpt.operator_text import (
    fuzzy_token_match,
    normalize_operator_message,
    operator_message_candidates,
    scrub_keyboard_smash,
    try_layout_flip,
)


def test_wrong_en_layout_to_russian_command():
    # Physical EN keys for «открой»: o→j, t→n, k→r, r→h, o→j, j→q → jnrhjq
    assert try_layout_flip("jnrhjq файл") == "открой файл"
    assert "открой" in normalize_operator_message("jnrhjq файл")


def test_layout_does_not_destroy_english_commands():
    assert try_layout_flip("open calculator") == "open calculator"
    assert normalize_operator_message("open the file") == "open the file"


def test_layout_preserves_russian_command_with_english_app_name():
    # Whole-message EN→RU flip used to mangle "Microsoft Edge" into gibberish and
    # break the native open-app route ("открой Ьшскщыщае Увпу").
    assert try_layout_flip("открой Microsoft Edge") == "открой Microsoft Edge"
    assert "microsoft edge" in normalize_operator_message("открой Microsoft Edge").lower()
    assert "открой" in normalize_operator_message("открой Microsoft Edge")


def test_layout_skips_paths():
    assert try_layout_flip(r"open C:\Users\Admin\file.txt") == r"open C:\Users\Admin\file.txt"


def test_keyboard_smash_keeps_word_islands():
    scrubbed = scrub_keyboard_smash(";;;; открой !!! файл ;;;;")
    assert "открой" in scrubbed
    assert "файл" in scrubbed


def test_fuzzy_token_match_typo():
    assert fuzzy_token_match("отркой", ["открой", "закрой"]) == "открой"
    assert fuzzy_token_match("откр", ["открой"]) is None  # too short budget/ambiguous


def test_candidates_include_normalized_forms():
    cands = operator_message_candidates("jnrhjq файл")
    assert any("открой" in item for item in cands)
