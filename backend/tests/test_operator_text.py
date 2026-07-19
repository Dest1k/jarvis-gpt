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


def test_conversational_russian_typed_on_english_layout():
    # "если вот так внезапно писать начнет/начнут" typed while EN layout is active.
    # yfxyen → "начнут" (keys for н-а-ч-н-у-т); yfxytn → "начнет".
    assert (
        try_layout_flip("tckb djn nfr dytpfgyj gbcfnm yfxyen")
        == "если вот так внезапно писать начнут"
    )
    assert (
        try_layout_flip("tckb djn nfr dytpfgyj gbcfnm yfxytn")
        == "если вот так внезапно писать начнет"
    )


def test_reminder_russian_typed_on_english_layout():
    # "напомни через 5 минут"
    flipped = try_layout_flip("yfgjvyb xthtp 5 vbyen")
    assert "напомни" in flipped
    assert "через" in flipped
    assert "5" in flipped
    assert "минут" in flipped


def test_english_typed_on_russian_layout():
    assert try_layout_flip("ыефегы") == "status"
    assert try_layout_flip("щзут") == "open"
    assert "open" in try_layout_flip("щзут calculator").lower() or try_layout_flip(
        "щзут calculator"
    ).startswith("open")


def test_layout_does_not_destroy_english_commands():
    assert try_layout_flip("open calculator") == "open calculator"
    assert normalize_operator_message("open the file") == "open the file"


def test_layout_preserves_russian_command_with_english_app_name():
    # Whole-message EN→RU flip used to mangle "Microsoft Edge" into gibberish and
    # break the native open-app route ("открой Ьшскщыщае Увпу").
    assert try_layout_flip("открой Microsoft Edge") == "открой Microsoft Edge"
    assert "microsoft edge" in normalize_operator_message("открой Microsoft Edge").lower()
    assert "открой" in normalize_operator_message("открой Microsoft Edge")
    # Wrong-layout verb + intentional English product name.
    assert try_layout_flip("jnrhjq Microsoft Edge") == "открой Microsoft Edge"


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
