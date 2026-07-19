"""Operator text normalization: confusables, keyboard layout, typos, garbage.

Used before intent detection so phrasing quirks (ё/е, NBSP, wrong layout,
light typos, and "face on keyboard" noise) do not defeat command recognition.
"""

from __future__ import annotations

import re
import unicodedata

# Same-meaning character variants operators commonly type.
_OPERATOR_CONFUSABLE_TRANSLATION: dict[int, str | None] = {
    0x00A0: " ",
    0x2000: " ",
    0x2001: " ",
    0x2002: " ",
    0x2003: " ",
    0x2004: " ",
    0x2005: " ",
    0x2006: " ",
    0x2007: " ",
    0x2008: " ",
    0x2009: " ",
    0x200A: " ",
    0x202F: " ",
    0x205F: " ",
    0x3000: " ",
    0x200B: None,
    0x200C: None,
    0x200D: None,
    0x2060: None,
    0xFEFF: None,
    0x0451: "е",
    0x0401: "Е",
}

# QWERTY physical keys → ЙЦУКЕН (and reverse) for common "wrong layout" paste/type.
_EN_TO_RU = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
)
_RU_TO_EN = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
)

_CYR = re.compile(r"[а-яА-ЯёЁ]")
_LAT = re.compile(r"[a-zA-Z]")
_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]{2,}")
_GARBAGE_RUN = re.compile(r"[^\w\s\-.,!?/:@#%&+=а-яА-ЯёЁ]{3,}", re.UNICODE)
_REPEATED = re.compile(r"(.)\1{4,}")


def fold_operator_confusables(text: str) -> str:
    """Fold same-meaning character variants (NBSP, ZW*, ё/е)."""

    folded = unicodedata.normalize("NFC", str(text or ""))
    return folded.translate(_OPERATOR_CONFUSABLE_TRANSLATION)


def _layout_score(text: str) -> tuple[int, int]:
    return len(_CYR.findall(text)), len(_LAT.findall(text))


# Command stems that confirm a wrong-layout flip actually produced intent text.
_RU_COMMAND_STEMS = (
    "открой",
    "открыть",
    "найди",
    "поищи",
    "сделай",
    "покажи",
    "запусти",
    "создай",
    "удали",
    "сохрани",
    "напиши",
    "прочитай",
    "скачай",
    "отправь",
    "память",
    "файл",
    "папку",
    "папка",
    "запусти",
    "выключи",
    "перезапусти",
    "статус",
    "помощь",
    "справка",
    "миссию",
    "миссия",
    "документ",
    "архив",
    "распакуй",
    "распаковать",
)
_EN_COMMAND_STEMS = (
    "open",
    "find",
    "search",
    "make",
    "show",
    "start",
    "create",
    "delete",
    "save",
    "write",
    "read",
    "download",
    "send",
    "memory",
    "file",
    "folder",
    "status",
    "help",
    "mission",
    "document",
    "archive",
    "extract",
    "restart",
    "stop",
    "run",
)


def _contains_stem(text: str, stems: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(stem in folded for stem in stems)


def try_layout_flip(text: str) -> str:
    """If the message looks typed in the wrong layout, return the flipped form.

    Only flip when the flipped candidate contains known command stems — pure
    English or pure Russian operator prose is left alone so legitimate
    bilingual commands are not destroyed.
    """

    raw = fold_operator_confusables(text)
    if not raw.strip():
        return raw
    # Do not flip paths / URLs / absolute Windows paths.
    if re.search(r"https?://|\\\\|[A-Za-z]:\\", raw):
        return raw
    cyr, lat = _layout_score(raw)
    if lat >= 3 and lat > cyr:
        flipped = raw.translate(_EN_TO_RU)
        if _contains_stem(flipped, _RU_COMMAND_STEMS) and not _contains_stem(
            raw, _EN_COMMAND_STEMS
        ):
            return flipped
    if cyr >= 3 and cyr > lat:
        flipped = raw.translate(_RU_TO_EN)
        if _contains_stem(flipped, _EN_COMMAND_STEMS) and not _contains_stem(
            raw, _RU_COMMAND_STEMS
        ):
            return flipped
    return raw


def operator_message_candidates(text: str) -> list[str]:
    """Return unique normalized candidates for intent matching (original + layout)."""

    base = fold_operator_confusables(text)
    scrubbed = scrub_keyboard_smash(base)
    flipped = try_layout_flip(scrubbed)
    candidates: list[str] = []
    for item in (scrubbed, flipped, normalize_operator_message(text)):
        cleaned = re.sub(r"\s+", " ", item).strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    # Always include confusable-only fold for exact legacy matchers.
    folded_only = re.sub(r"\s+", " ", base).strip()
    if folded_only and folded_only not in candidates:
        candidates.insert(0, folded_only)
    return candidates


def scrub_keyboard_smash(text: str) -> str:
    """Reduce 'face on keyboard' noise while keeping word islands.

    Strips long non-word runs and collapses extreme character repetition, then
    keeps tokens that look like real words (length ≥ 2).
    """

    raw = fold_operator_confusables(text)
    raw = _GARBAGE_RUN.sub(" ", raw)
    raw = _REPEATED.sub(r"\1\1", raw)
    tokens = _WORD.findall(raw)
    if not tokens:
        return raw.strip()
    # If almost everything was garbage, reassemble only the word islands.
    if len("".join(tokens)) < max(4, int(len(re.sub(r"\s+", "", raw)) * 0.35)):
        return " ".join(tokens)
    return re.sub(r"\s+", " ", raw).strip()


def levenshtein(a: str, b: str, *, limit: int = 4) -> int:
    """Bounded Levenshtein distance; returns limit+1 when already worse."""

    a = a.casefold()
    b = b.casefold()
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            val = min(ins, delete, sub)
            cur.append(val)
            if val < row_min:
                row_min = val
        if row_min > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def fuzzy_token_match(
    token: str, candidates: list[str], *, max_dist: int | None = None
) -> str | None:
    """Return the unique closest candidate within a length-scaled edit budget."""

    token = (token or "").strip().casefold()
    if not token or len(token) < 3:
        return None
    if max_dist is not None:
        budget = max_dist
    elif len(token) <= 5:
        budget = 1
    elif len(token) <= 9:
        budget = 2
    else:
        budget = 3
    best: list[tuple[int, str]] = []
    for cand in candidates:
        d = levenshtein(token, cand, limit=budget)
        if d <= budget:
            best.append((d, cand))
    if not best:
        return None
    best.sort()
    # Require unique winner at the best distance.
    winners = [c for d, c in best if d == best[0][0]]
    if len(winners) != 1:
        return None
    return winners[0]


def normalize_operator_message(text: str) -> str:
    """Full normalization pipeline for operator-facing matching."""

    step = fold_operator_confusables(text)
    step = scrub_keyboard_smash(step)
    step = try_layout_flip(step)
    step = fold_operator_confusables(step)  # after layout flip, ё may reappear
    return re.sub(r"\s+", " ", step).strip()
