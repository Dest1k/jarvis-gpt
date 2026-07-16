"""Robustness of operator-command understanding to phrasing/character quirks.

Copy-pasted or mobile-typed requests carry characters that look identical to the
operator but defeat the command matchers: non-breaking or zero-width spaces around
words (breaking ``\\b`` boundaries) and ``ё`` where the patterns expect ``е``. The
fold below normalizes those away before intent detection, so the same command is
recognized regardless of how it was typed.
"""

from __future__ import annotations

from jarvis_gpt.agent import (
    _fold_operator_confusables,
    _native_action_from_message,
    _operator_action_scopes,
)

NBSP = chr(0x00A0)
THIN_SPACE = chr(0x2009)
ZWSP = chr(0x200B)
ZWJ = chr(0x200D)
BOM = chr(0xFEFF)
YO = chr(0x0451)  # ё
YO_UP = chr(0x0401)  # Ё


def test_fold_removes_zero_width_characters():
    folded = _fold_operator_confusables(f"от{ZWSP}крой{ZWJ} файл{BOM}")
    assert ZWSP not in folded
    assert ZWJ not in folded
    assert BOM not in folded
    assert folded == "открой файл"


def test_fold_normalizes_unicode_spaces_to_plain_space():
    folded = _fold_operator_confusables(f"открой{NBSP}калькулятор{THIN_SPACE}сейчас")
    assert NBSP not in folded
    assert THIN_SPACE not in folded
    assert folded == "открой калькулятор сейчас"


def test_fold_maps_yo_to_ye():
    assert _fold_operator_confusables(f"посчита{YO}т") == "посчитает"
    assert _fold_operator_confusables(f"{YO_UP}лка") == "Елка"


def test_fold_preserves_ascii_and_latin_commands():
    # Latin command words must be untouched (the toolset is bilingual).
    assert _fold_operator_confusables("open the file at C:/tmp/x.txt") == (
        "open the file at C:/tmp/x.txt"
    )


def test_command_scopes_survive_non_breaking_space():
    base = _operator_action_scopes("открой калькулятор")
    quirky = _operator_action_scopes(f"открой{NBSP}калькулятор")
    assert "explicit" in quirky
    assert quirky == base


def test_command_scopes_survive_leading_zero_width():
    assert "explicit" in _operator_action_scopes(f"{ZWSP}открой калькулятор")


def test_calculation_scope_survives_yo():
    scopes = _operator_action_scopes(f"посчитай 2+2 в кальку{YO}ляторе")
    assert "explicit" in scopes
    assert "type" in scopes  # math verbs authorize native typing


def test_native_action_survives_non_breaking_space():
    action = _native_action_from_message(f"открой{NBSP}блокнот")
    assert action is not None


def test_negation_still_wins_after_fold():
    # Folding must not turn a refusal into a command.
    assert _operator_action_scopes(f"не{NBSP}открывай калькулятор") == frozenset()
