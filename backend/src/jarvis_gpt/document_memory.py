"""Durable document recall over Jarvis file storage and document_surfer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .document_surfer import (
    DocumentSurferError,
    JarvisDocumentSurfer,
    is_document_path_supported,
)
from .storage import JarvisStorage

DOCUMENT_MEMORY_PROTOCOL = "jarvis.document-memory.v1"
DEFAULT_TOTAL_TEXT_CHARS = 18_000


@dataclass(frozen=True, slots=True)
class DocumentDateScope:
    """A resolved date window for recalling documents by their upload date.

    ``start_utc``/``end_utc`` are ISO-8601 ``+00:00`` strings comparable directly against
    ``files.created_at``; ``end_utc`` is exclusive. ``conclude`` is True when the operator
    asked for an analysis/verdict over the documents rather than a bare listing.
    """

    start_utc: str
    end_utc: str
    label: str
    conclude: bool
    type_exts: tuple[str, ...] = ()  # empty = any file type
    topic: str = ""  # empty = no topic filter


_DATE_SCOPE_DOC_NOUNS = (
    "документ", "докумен", "файл", "вложен", "document", "file", "attachment",
)
_DATE_SCOPE_CONCLUDE_MARKERS = (
    "вывод", "выводы", "проанализир", "анализ", "что следует", "к чему",
    "итог", "обобщ", "резюм", "подытож", "сделай вывод", "суть",
    "summary", "summarize", "analyze", "analyse", "conclusion", "conclude",
    # Cross-document comparison verbs also require reading the documents (conclude mode).
    "сравн", "сопостав", "противоречи", "различи", "различа", "отличи", "отлича",
    "разниц", "что общего", "compare", "differ", "contradict", "versus",
)
# Month stems: "март" is distinct from "ма[йяе]" (may forms), so no conflict.
_MONTH_STEMS: tuple[tuple[str, int], ...] = (
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("ма[йяе]", 5),
    ("июн", 6), ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10),
    ("ноябр", 11), ("декабр", 12),
)
_MONTH_ALT = "|".join(stem for stem, _ in _MONTH_STEMS)
_MONTH_NAMES_RU = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


# File-type words → the filename extensions they select. Ordered longest/most-specific
# first is unnecessary (we union all matches). Bare "md" is avoided (too many substrings).
_DOC_TYPE_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("xlsx", ("xlsx", "xls")),
    ("excel", ("xlsx", "xls")),
    ("эксель", ("xlsx", "xls")),
    ("таблиц", ("xlsx", "xls", "csv")),
    ("csv", ("csv",)),
    ("docx", ("docx", "doc")),
    ("word", ("docx", "doc")),
    ("ворд", ("docx", "doc")),
    ("pdf", ("pdf",)),
    ("pptx", ("pptx", "ppt")),
    ("powerpoint", ("pptx", "ppt")),
    ("презентац", ("pptx", "ppt")),
    ("json", ("json",)),
    ("markdown", ("md",)),
    ("текстов", ("txt", "md")),
    ("txt", ("txt",)),
)


def _detect_document_types(text: str) -> tuple[str, ...]:
    exts: set[str] = set()
    for token, extensions in _DOC_TYPE_TOKENS:
        if token in text:
            exts.update(extensions)
    return tuple(sorted(exts))


def _filename_extension(name: str) -> str:
    stem = str(name or "")
    return stem.rsplit(".", 1)[-1].lower() if "." in stem else ""


# Explicit topic markers only (не bare "о"/"в"), so a date query is not mis-read as a
# topic filter. The captured phrase is cut before any date preposition.
_TOPIC_MARKER_RE = re.compile(
    r"(?:\bпро\b|\bобо\b|насч[её]т|по\s+теме|касательно|содержащ\w*|где\s+(?:есть|упомина\w+))"
    r"\s+(.+)"
)
_TOPIC_DATE_CUT_RE = re.compile(
    r"\b(?:за|с|со|между|на|в|вчера|сегодня|позавчера|прошл\w*|эт\w*|последн\w*)\b|\d"
)


def _extract_topic(text: str) -> str:
    match = _TOPIC_MARKER_RE.search(text)
    if not match:
        return ""
    phrase = _TOPIC_DATE_CUT_RE.split(match.group(1))[0]
    return " ".join(phrase.split())[:60]


def _month_from_word(word: str) -> int | None:
    for stem, number in _MONTH_STEMS:
        if re.match(rf"^{stem}", word):
            return number
    return None


def _local_midnight(reference: datetime, year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=reference.tzinfo)


def _year_not_in_future(reference: datetime, month: int, day: int) -> int:
    """Pick a year for a bare "DD month": current, or last year if that is still ahead."""

    try:
        candidate = _local_midnight(reference, reference.year, month, day)
    except ValueError:
        return reference.year
    return reference.year if candidate <= reference else reference.year - 1


def _match_document_date_window(
    text: str, now: datetime
) -> tuple[datetime, datetime, str] | None:
    today = _local_midnight(now, now.year, now.month, now.day)
    # Range first: "с 10 [июля] по 16 июля [2026]" / "между 10 и 16 июля". The end day is
    # inclusive, so the window extends to the start of the following day.
    rng = re.search(
        rf"(?:с|со|между)\s+(\d{{1,2}})(?:\s+({_MONTH_ALT})[а-яё]*)?\s+(?:по|и|до)\s+"
        rf"(\d{{1,2}})\s+({_MONTH_ALT})[а-яё]*(?:\s+(\d{{4}}))?",
        text,
    )
    if rng:
        day1, day2 = int(rng[1]), int(rng[3])
        month2 = _month_from_word(rng[4]) or 0
        month1 = _month_from_word(rng[2]) if rng[2] else month2
        if month1 and month2:
            year = int(rng[5]) if rng[5] else _year_not_in_future(now, month2, day2)
            try:
                start = _local_midnight(now, year, month1, day1)
                end = _local_midnight(now, year, month2, day2) + timedelta(days=1)
            except ValueError:
                return None
            if end > start:
                label = (
                    f"{day1} {_MONTH_NAMES_RU[month1]} — "
                    f"{day2} {_MONTH_NAMES_RU[month2]} {year}"
                )
                return start, end, label
    if "позавчера" in text:
        start = today - timedelta(days=2)
        return start, start + timedelta(days=1), "позавчера"
    if "вчера" in text:
        start = today - timedelta(days=1)
        return start, start + timedelta(days=1), "вчера"
    if "сегодня" in text:
        return today, today + timedelta(days=1), "сегодня"
    # ISO 2026-07-15
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        try:
            start = _local_midnight(now, int(iso[1]), int(iso[2]), int(iso[3]))
        except ValueError:
            return None
        return start, start + timedelta(days=1), start.strftime("%Y-%m-%d")
    # DD.MM(.YYYY)
    dmy = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", text)
    if dmy:
        day, month = int(dmy[1]), int(dmy[2])
        year = int(dmy[3]) if dmy[3] else _year_not_in_future(now, month, day)
        if year < 100:
            year += 2000
        try:
            start = _local_midnight(now, year, month, day)
        except ValueError:
            return None
        return start, start + timedelta(days=1), f"{day:02d}.{month:02d}.{year}"
    # DD <month> [YYYY]
    dm = re.search(rf"\b(\d{{1,2}})\s+({_MONTH_ALT})[а-яё]*(?:\s+(\d{{4}}))?", text)
    if dm:
        day = int(dm[1])
        month = _month_from_word(dm[2]) or 0
        if month:
            year = int(dm[3]) if dm[3] else _year_not_in_future(now, month, day)
            try:
                start = _local_midnight(now, year, month, day)
            except ValueError:
                return None
            label = f"{day} {_MONTH_NAMES_RU[month]} {year}"
            return start, start + timedelta(days=1), label
    # прошлая / эта неделя
    if re.search(r"прошл\w*\s+недел", text) or "на прошлой неделе" in text:
        monday = today - timedelta(days=today.weekday() + 7)
        return monday, monday + timedelta(days=7), "прошлая неделя"
    if re.search(r"(эт\w*|текущ\w*)\s+недел", text) or "на этой неделе" in text:
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=7), "эта неделя"
    # прошлый / этот месяц
    if re.search(r"прошл\w*\s+месяц", text) or "в прошлом месяце" in text:
        first = _local_midnight(now, now.year, now.month, 1)
        prev_end = first
        prev_start = _local_midnight(
            now, first.year - 1 if first.month == 1 else first.year,
            12 if first.month == 1 else first.month - 1, 1,
        )
        return prev_start, prev_end, "прошлый месяц"
    if re.search(r"(эт\w*|текущ\w*)\s+месяц", text):
        first = _local_midnight(now, now.year, now.month, 1)
        nxt = _local_midnight(
            now, now.year + 1 if now.month == 12 else now.year,
            1 if now.month == 12 else now.month + 1, 1,
        )
        return first, nxt, "этот месяц"
    # за последние N дней / за N дней
    days = re.search(r"последн\w*\s+(\d{1,3})\s+дн|за\s+(\d{1,3})\s+дн", text)
    if days:
        count = int(days[1] or days[2] or 0)
        if 1 <= count <= 366:
            start = today - timedelta(days=count - 1)
            return start, today + timedelta(days=1), f"последние {count} дн."
    if re.search(r"(за|последн\w*)\s+недел", text):
        start = today - timedelta(days=6)
        return start, today + timedelta(days=1), "последняя неделя"
    # Bare month name last ("за июль", "в июле") -> that whole calendar month. Runs after
    # the DD-month pattern, so "15 июля" stays a single day.
    bare_month = re.search(rf"\b({_MONTH_ALT})[а-яё]*", text)
    if bare_month:
        month = _month_from_word(bare_month[1]) or 0
        if month:
            first_this_year = _local_midnight(now, now.year, month, 1)
            year = now.year if first_this_year <= now else now.year - 1
            start = _local_midnight(now, year, month, 1)
            end = _local_midnight(
                now, year + 1 if month == 12 else year, 1 if month == 12 else month + 1, 1
            )
            return start, end, f"{_MONTH_NAMES_RU[month]} {year}"
    return None


def parse_document_date_scope(
    message: str, *, now: datetime | None = None
) -> DocumentDateScope | None:
    """Detect a date-scoped document query and resolve its UTC window, else None.

    Requires both a document noun ("документ"/"файл"/…) and a parseable date expression,
    so a generic dated question is not hijacked. Relative dates ("вчера") resolve in the
    machine's local timezone, then map to a UTC ``created_at`` window.
    """

    text = " ".join(str(message or "").casefold().split())
    if not text:
        return None
    type_exts = _detect_document_types(text)
    # A document noun ("документ"/"файл"/…) or a file-type word ("xlsx", "таблицы")
    # marks this as a document question; without either it is a generic dated query.
    if not type_exts and not any(noun in text for noun in _DATE_SCOPE_DOC_NOUNS):
        return None
    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.astimezone()
    window = _match_document_date_window(text, reference)
    if window is None:
        return None
    start, end, label = window
    conclude = any(marker in text for marker in _DATE_SCOPE_CONCLUDE_MARKERS)
    return DocumentDateScope(
        start_utc=start.astimezone(UTC).isoformat(timespec="seconds"),
        end_utc=end.astimezone(UTC).isoformat(timespec="seconds"),
        label=label,
        conclude=conclude,
        type_exts=type_exts,
        topic=_extract_topic(text),
    )

_RECALL_NOISE_STEMS = (
    "анализ",
    "вспомн",
    "выдай",
    "дай",
    "документ",
    "достан",
    "загруз",
    "индекс",
    "кратк",
    "найд",
    "недавн",
    "памят",
    "покаж",
    "последн",
    "предыдущ",
    "присыл",
    "прочит",
    "прошл",
    "разбер",
    "резюм",
    "сводк",
    "содержан",
    "сохран",
    "файл",
    "analy",
    "document",
    "file",
    "index",
    "memory",
    "earlier",
    "last",
    "latest",
    "previous",
    "recall",
    "recent",
    "remember",
    "save",
    "sent",
    "summar",
    "upload",
)
_RECALL_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "for",
    "from",
    "in",
    "mine",
    "my",
    "of",
    "our",
    "the",
    "to",
    "а",
    "в",
    "все",
    "дай",
    "для",
    "и",
    "из",
    "к",
    "мне",
    "мой",
    "моя",
    "моё",
    "мое",
    "моего",
    "мою",
    "на",
    "о",
    "об",
    "по",
    "про",
    "с",
    "что",
}
_RECENT_MARKERS = (
    "последн",
    "недавн",
    "предыдущ",
    "прошл",
    "ранее",
    "до этого",
    "last",
    "latest",
    "recent",
    "previous",
    "earlier",
)
_MEMORY_MARKERS = (
    "памят",
    "загруз",
    "присыл",
    "отправл",
    "сохран",
    "индекс",
    "memory",
    "upload",
    "sent",
    "saved",
    "indexed",
)
_MULTI_MARKERS = (
    "все документ",
    "все файл",
    "несколько документ",
    "документы",
    "файлы",
    "all documents",
    "all files",
    "documents",
    "files",
    "сравни",
    "compare",
    "оба",
    "both",
    " and ",
    " и ",
)
_GENERIC_DOCUMENT_TERMS = {
    "agreement",
    "agreements",
    "contract",
    "contracts",
    "doc",
    "docs",
    "docx",
    "document",
    "documents",
    "file",
    "files",
    "presentation",
    "presentations",
    "report",
    "reports",
    "spreadsheet",
    "spreadsheets",
    "workbook",
    "workbooks",
}
_GENERIC_DOCUMENT_STEMS = (
    "договор",
    "документ",
    "отч",
    "презентац",
    "таблиц",
    "файл",
)
# Instruction / filler tokens that must not defeat an exact filename/ID match.
_IDENTITY_NOISE_TERMS = {
    "attached",
    "attachment",
    "content",
    "field",
    "find",
    "give",
    "its",
    "key",
    "marker",
    "name",
    "named",
    "only",
    "please",
    "return",
    "say",
    "show",
    "tell",
    "that",
    "this",
    "value",
    "which",
    "значение",
    "значения",
    "ключ",
    "ключа",
    "маркер",
    "найди",
    "назови",
    "поле",
    "поля",
    "приложенном",
    "приложенный",
    "приложенная",
    "приложенные",
    "скажи",
    "только",
    "указано",
    "указан",
    "верни",
    "его",
    "её",
    "ее",
    "ранее",
    "загружен",
    "загружена",
    "загружено",
    "загружены",
    "загруженный",
    "загруженная",
    "загруженное",
    "загруженные",
    "загруженном",
    "загруженную",
    "загруженных",
}
_FILE_ID_RE = re.compile(r"\bfile_[0-9a-fA-F]{8,}\b")
_FILENAME_MENTION_RE = re.compile(
    r"(?<![\w./\\])([\w.-]+\.[A-Za-z0-9]{1,12})(?![\w./\\])",
    flags=re.UNICODE,
)


class DocumentMemory:
    """Resolve persisted document identities, then read and analyze their sources."""

    def __init__(self, *, storage: JarvisStorage, surfer: JarvisDocumentSurfer) -> None:
        self.storage = storage
        self.surfer = surfer

    def recall(
        self,
        query: str,
        *,
        file_ids: list[str] | None = None,
        max_files: int = 3,
        max_chars: int = 60_000,
        focus: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        list_only: bool = False,
        type_exts: tuple[str, ...] | list[str] | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        query_clean = " ".join(str(query or "").split()).strip()
        date_scoped = bool(date_from and date_to)
        if not query_clean and not file_ids and not date_scoped:
            raise ValueError("query, file_ids or a date range is required")
        max_files = max(1, min(8, int(max_files)))
        max_chars = max(1_000, min(80_000, int(max_chars)))
        if date_scoped:
            return self._recall_by_date(
                query_clean,
                date_from=str(date_from),
                date_to=str(date_to),
                list_only=list_only,
                focus=focus,
                max_files=max_files,
                max_chars=max_chars,
                type_exts=tuple(type_exts or ()),
                topic=" ".join(str(topic or "").split()),
            )
        retrieval_query = _retrieval_query(query_clean)
        candidates, selection_mode, selection_errors = self._select_candidates(
            query_clean,
            retrieval_query=retrieval_query,
            file_ids=file_ids or [],
            limit=max_files,
        )
        selection_truncated = len(candidates) > max_files or len(file_ids or []) > max_files
        ambiguous = _selection_is_ambiguous(
            candidates,
            query=query_clean,
            selection_mode=selection_mode,
        )
        if ambiguous:
            return {
                "protocol": DOCUMENT_MEMORY_PROTOCOL,
                "ok": False,
                "query": query_clean,
                "retrieval_query": retrieval_query or None,
                "selection": {
                    "mode": selection_mode,
                    "matched": len(candidates),
                    "analyzed": 0,
                    "ambiguous": True,
                    "truncated": selection_truncated,
                },
                "sources": [_public_source(item, include_chunks=False) for item in candidates],
                "passages": [],
                "analyses": [],
                "corpus": None,
                "errors": [
                    *selection_errors,
                    {
                        "error": "ambiguous document match; pass an explicit file_id",
                        "stage": "selection",
                    },
                ],
                "trust": "untrusted_document_data",
            }
        candidates = candidates[:max_files]
        sources: list[dict[str, Any]] = []
        paths: list[Path] = []
        errors = list(selection_errors)
        for candidate in candidates:
            path = Path(str(candidate.get("stored_path") or "")).resolve(strict=False)
            if not path.exists() or not path.is_file():
                errors.append(
                    {
                        "file_id": candidate.get("id"),
                        "name": candidate.get("name"),
                        "error": "stored file is missing",
                    }
                )
                continue
            if not is_document_path_supported(path, str(candidate.get("mime_type") or "")):
                errors.append(
                    {
                        "file_id": candidate.get("id"),
                        "name": candidate.get("name"),
                        "error": "stored file is not a supported document",
                    }
                )
                continue
            paths.append(path)
            sources.append(_public_source(candidate))

        if not paths:
            return {
                "protocol": DOCUMENT_MEMORY_PROTOCOL,
                "ok": False,
                "query": query_clean,
                "retrieval_query": retrieval_query or None,
                "selection": {
                    "mode": selection_mode,
                    "matched": len(candidates),
                    "ambiguous": False,
                    "truncated": selection_truncated,
                },
                "sources": sources,
                "passages": [],
                "analyses": [],
                "corpus": None,
                "errors": errors,
                "trust": "untrusted_document_data",
            }

        focus_clean = " ".join(str(focus or retrieval_query or "").split()).strip()
        total_budget = min(
            DEFAULT_TOTAL_TEXT_CHARS,
            max_chars * len(paths),
        )
        per_file_budget = max(1_000, min(max_chars, total_budget // len(paths)))
        passages: list[dict[str, Any]] = []
        analyses: list[dict[str, Any]] = []
        analyzed_paths: list[Path] = []
        for source, path in zip(sources, paths, strict=True):
            try:
                read_result = self.surfer.read(path, max_chars=max_chars)
                analysis = self.surfer.analyze(
                    path,
                    max_chars=max_chars,
                    instruction=focus_clean,
                )
            except DocumentSurferError as exc:
                errors.append(
                    {
                        "file_id": source["file_id"],
                        "name": source["name"],
                        "error": str(exc),
                    }
                )
                continue
            text = str(read_result.get("text") or "")
            excerpt = _bounded_document_text(text, per_file_budget)
            read_document = (
                read_result.get("document")
                if isinstance(read_result.get("document"), dict)
                else {}
            )
            passages.append(
                {
                    "file_id": source["file_id"],
                    "name": source["name"],
                    "content": excerpt,
                    "content_chars": len(text),
                    "truncated": bool(read_document.get("truncated"))
                    or len(text) > len(excerpt),
                }
            )
            analyses.append(_public_analysis(source, analysis))
            analyzed_paths.append(path)

        corpus = None
        if analyzed_paths:
            try:
                corpus_result = self.surfer.summarize_corpus(
                    analyzed_paths,
                    focus=focus_clean or None,
                    max_chars=max_chars,
                )
                corpus = _public_corpus(corpus_result, sources, paths)
            except DocumentSurferError as exc:
                errors.append({"error": str(exc), "stage": "corpus_summary"})

        return {
            "protocol": DOCUMENT_MEMORY_PROTOCOL,
            "ok": bool(analyses or passages),
            "query": query_clean,
            "retrieval_query": retrieval_query or None,
            "selection": {
                "mode": selection_mode,
                "matched": len(sources),
                "analyzed": len(analyses),
                "ambiguous": False,
                "truncated": selection_truncated,
                "limit": max_files,
            },
            "sources": sources,
            "passages": passages,
            "analyses": analyses,
            "corpus": corpus,
            "errors": errors,
            "trust": "untrusted_document_data",
        }

    def _recall_by_date(
        self,
        query: str,
        *,
        date_from: str,
        date_to: str,
        list_only: bool,
        focus: str | None,
        max_files: int,
        max_chars: int,
        type_exts: tuple[str, ...] = (),
        topic: str = "",
    ) -> dict[str, Any]:
        """Recall documents by upload date: list them, or read+conclude over them."""

        records = self.storage.list_files_in_range(date_from, date_to, limit=200)
        wanted = {ext.lower() for ext in type_exts}
        documents = [
            {
                "file_id": str(record.get("id") or ""),
                "name": str(record.get("name") or ""),
                "created_at": str(record.get("created_at") or ""),
                "mime_type": str(record.get("mime_type") or ""),
                "size": int(record.get("size") or 0),
                "status": str(record.get("status") or ""),
            }
            for record in records
            if not wanted or _filename_extension(str(record.get("name") or "")) in wanted
        ]
        if topic:
            # Keep only documents matching the topic by name or indexed content (FTS).
            matching_ids = {
                str(hit.get("id") or "")
                for hit in self.storage.search_files(topic, limit=50)
            }
            topic_lower = topic.lower()
            documents = [
                doc
                for doc in documents
                if doc["file_id"] in matching_ids or topic_lower in doc["name"].lower()
            ]
        scope = {
            "from": date_from,
            "to": date_to,
            "count": len(documents),
            "types": sorted(wanted) or None,
            "topic": topic or None,
        }
        base: dict[str, Any] = {
            "protocol": DOCUMENT_MEMORY_PROTOCOL,
            "ok": True,
            "query": query,
            "date_scope": scope,
            "documents": documents,
            "selection": {
                "mode": "date",
                "matched": len(documents),
                "analyzed": 0,
                "ambiguous": False,
                "truncated": len(records) >= 200,
            },
            "sources": [],
            "passages": [],
            "analyses": [],
            "corpus": None,
            "errors": [],
            "trust": "untrusted_document_data",
        }
        if list_only or not documents:
            base["mode"] = "list"
            return base
        # Conclude mode reuses the full read/analyze/synthesis path via explicit file_ids,
        # so the date branch adds no duplicate document-reading logic.
        file_ids = [doc["file_id"] for doc in documents[:max_files] if doc["file_id"]]
        analysis = self.recall(
            query or focus or "Сделай вывод по этим документам.",
            file_ids=file_ids,
            max_files=max_files,
            max_chars=max_chars,
            focus=focus,
        )
        analysis["date_scope"] = scope
        analysis["documents"] = documents
        analysis["mode"] = "conclude"
        # Documents provably existed for the day; a read failure downgrades to a listing
        # with errors rather than reporting "nothing found".
        if not analysis.get("ok"):
            analysis["ok"] = True
        return analysis

    def _select_candidates(
        self,
        query: str,
        *,
        retrieval_query: str,
        file_ids: list[str],
        limit: int,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        errors: list[dict[str, Any]] = []
        explicit_ids = list(file_ids or [])
        for mentioned_id in _file_ids_mentioned(query):
            if mentioned_id not in explicit_ids:
                explicit_ids.append(mentioned_id)
        if explicit_ids:
            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw_file_id in explicit_ids[:limit]:
                file_id = str(raw_file_id or "").strip()
                if not file_id or file_id in seen:
                    continue
                seen.add(file_id)
                record = self.storage.get_file(file_id)
                if record is None:
                    errors.append({"file_id": file_id, "error": "file not found"})
                    continue
                candidates.append(
                    {
                        **record,
                        "match_sources": ["explicit_file_id"],
                        "matched_terms": [],
                        "match_score": 100.0,
                        "matched_chunks": [],
                    }
                )
            return candidates, "explicit", errors

        ranked: dict[str, dict[str, Any]] = {}
        # Exact basename mentions (including Unicode) are first-class identity.
        for mention in _filenames_mentioned(query):
            for record in self.storage.search_files(mention, limit=limit * 3):
                name = str(record.get("name") or "")
                if not _names_match_exactly(name, mention):
                    continue
                ranked[str(record["id"])] = {
                    **record,
                    "match_sources": sorted(
                        {
                            *list(record.get("match_sources") or []),
                            "name",
                            "exact_filename",
                        }
                    ),
                    "matched_terms": sorted(
                        {
                            *list(record.get("matched_terms") or []),
                            mention,
                        },
                        key=str.casefold,
                    ),
                    "match_score": max(float(record.get("match_score") or 0.0), 50.0),
                }

        if retrieval_query:
            for record in self.storage.search_files(retrieval_query, limit=limit * 3):
                existing = ranked.get(str(record["id"]))
                if existing is None or float(record.get("match_score") or 0.0) > float(
                    existing.get("match_score") or 0.0
                ):
                    ranked[str(record["id"])] = record

        # A remembered filename may consist only of otherwise generic words
        # (for example "summary.docx").  Use the original request only for
        # filename-backed candidates so action words cannot match arbitrary text.
        for record in self.storage.search_files(query, limit=limit * 3):
            if "name" not in set(record.get("match_sources") or []):
                continue
            existing = ranked.get(str(record["id"]))
            if existing is None or float(record.get("match_score") or 0.0) > float(
                existing.get("match_score") or 0.0
            ):
                ranked[str(record["id"])] = record

        identity_terms = _identity_terms(retrieval_query or query)
        candidates = [
            _with_identity_match(item, identity_terms)
            for item in sorted(
                ranked.values(),
                key=lambda item: -float(item.get("match_score") or 0.0),
            )
            if _record_is_supported_document(item)
            and _passes_identity_threshold(item, identity_terms, query=query)
        ][: max(limit + 1, 2)]
        if candidates:
            exact_name_count = len(_filenames_mentioned(query))
            # "earlier/last" collapses only when the operator did not name multiple files.
            if (
                _requests_recent(query)
                and not _requests_multiple(query)
                and exact_name_count <= 1
            ):
                candidates = [max(candidates, key=_document_recency_key)]
            return candidates, "search", errors

        if _allows_recent_fallback(query, retrieval_query):
            recent_mode = "recent" if _requests_recent(query) else "memory"
            recent_limit = (
                1
                if recent_mode == "recent" and not _requests_multiple(query)
                else max(limit + 1, 2)
            )
            recent = []
            offset = 0
            while len(recent) < recent_limit:
                batch = self.storage.list_files(limit=50, offset=offset)
                if not batch:
                    break
                for record in batch:
                    if not _record_is_supported_document(record):
                        continue
                    recent.append(
                        {
                            **record,
                            "match_sources": ["recent"],
                            "matched_terms": [],
                            "match_score": 0.0,
                            "matched_chunks": [],
                        }
                    )
                    if len(recent) >= recent_limit:
                        break
                offset += len(batch)
                if len(batch) < 50:
                    break
            return recent, recent_mode, errors
        return [], "search", errors


def _public_source(
    record: dict[str, Any],
    *,
    include_chunks: bool = True,
) -> dict[str, Any]:
    return {
        "file_id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "mime_type": str(record.get("mime_type") or ""),
        "status": str(record.get("status") or ""),
        "chunk_count": int(record.get("chunk_count") or 0),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "match_sources": list(record.get("match_sources") or []),
        "matched_terms": list(record.get("matched_terms") or []),
        "match_score": float(record.get("match_score") or 0.0),
        "matched_chunks": (
            list(record.get("matched_chunks") or [])[:4] if include_chunks else []
        ),
        "identity_terms": list(record.get("identity_terms") or []),
        "identity_matched_terms": list(record.get("identity_matched_terms") or []),
        "identity_coverage": float(record.get("identity_coverage") or 0.0),
    }


def _record_is_supported_document(record: dict[str, Any]) -> bool:
    path = Path(str(record.get("stored_path") or "")).resolve(strict=False)
    try:
        return bool(
            path.exists()
            and path.is_file()
            and is_document_path_supported(path, str(record.get("mime_type") or ""))
        )
    except OSError:
        return False


def _identity_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[\w-]+", query, flags=re.UNICODE):
        normalized = token.casefold().strip("._-")
        if (
            not normalized
            or _is_generic_document_term(normalized)
            or normalized in _IDENTITY_NOISE_TERMS
            or any(normalized.startswith(stem) for stem in _RECALL_NOISE_STEMS)
        ):
            continue
        if normalized not in terms:
            terms.append(normalized)
    return terms


def _is_generic_document_term(term: str) -> bool:
    return term in _GENERIC_DOCUMENT_TERMS or any(
        term.startswith(stem) for stem in _GENERIC_DOCUMENT_STEMS
    )


def _file_ids_mentioned(query: str) -> list[str]:
    return list(dict.fromkeys(_FILE_ID_RE.findall(str(query or ""))))


def _filenames_mentioned(query: str) -> list[str]:
    mentions: list[str] = []
    for match in _FILENAME_MENTION_RE.findall(str(query or "")):
        cleaned = match.strip().strip("\"'`")
        if cleaned and cleaned not in mentions:
            mentions.append(cleaned)
    return mentions


def _names_match_exactly(left: str, right: str) -> bool:
    left_name = Path(str(left or "")).name.casefold()
    right_name = Path(str(right or "")).name.casefold()
    return bool(left_name and right_name and left_name == right_name)


def _record_has_exact_identity(record: dict[str, Any], query: str) -> bool:
    """True when the operator named this file or its stable source id exactly."""

    query_cf = str(query or "").casefold()
    if not query_cf:
        return False
    file_id = str(record.get("id") or "").casefold()
    if file_id and file_id in query_cf:
        return True
    name = str(record.get("name") or "")
    if name and name.casefold() in query_cf:
        return True
    for mention in _filenames_mentioned(query):
        if _names_match_exactly(name, mention):
            return True
    return "exact_filename" in set(record.get("match_sources") or [])


def _identity_matches(record: dict[str, Any], identity_terms: list[str]) -> list[str]:
    matched = {str(term).casefold() for term in record.get("matched_terms") or []}
    name_cf = str(record.get("name") or "").casefold()
    for token in re.findall(r"[\w.-]+", name_cf, flags=re.UNICODE):
        cleaned = token.strip("._-")
        if cleaned:
            matched.add(cleaned)
    return [term for term in identity_terms if term in matched]


def _passes_identity_threshold(
    record: dict[str, Any],
    identity_terms: list[str],
    *,
    query: str = "",
) -> bool:
    if _record_has_exact_identity(record, query):
        return True
    if not identity_terms:
        return True
    matched = _identity_matches(record, identity_terms)
    required = (
        len(identity_terms)
        if len(identity_terms) <= 2
        else max(2, (len(identity_terms) + 1) // 2)
    )
    return len(matched) >= required


def _with_identity_match(
    record: dict[str, Any],
    identity_terms: list[str],
) -> dict[str, Any]:
    matched = _identity_matches(record, identity_terms)
    coverage = len(matched) / len(identity_terms) if identity_terms else 0.0
    return {
        **record,
        "identity_terms": identity_terms,
        "identity_matched_terms": matched,
        "identity_coverage": round(coverage, 4),
    }


def _selection_is_ambiguous(
    candidates: list[dict[str, Any]],
    *,
    query: str,
    selection_mode: str,
) -> bool:
    if (
        selection_mode not in {"memory", "search"}
        or len(candidates) < 2
        or _requests_multiple(query)
    ):
        return False
    exact_mentions = _filenames_mentioned(query)
    if len(exact_mentions) >= 2:
        exact_hits = [
            item for item in candidates if _record_has_exact_identity(item, query)
        ]
        # Operator named multiple concrete files: return all exact hits, do not
        # ask for clarification as if they were competing alternatives.
        if len(exact_hits) >= 2:
            return False
    first, second = candidates[:2]
    coverage_gap = float(first.get("identity_coverage") or 0.0) - float(
        second.get("identity_coverage") or 0.0
    )
    if coverage_gap >= 0.34:
        return False
    score_gap = float(first.get("match_score") or 0.0) - float(
        second.get("match_score") or 0.0
    )
    first_named = "name" in set(first.get("match_sources") or [])
    second_named = "name" in set(second.get("match_sources") or [])
    if first_named and not second_named and score_gap >= 1.0:
        return False
    return score_gap < 1.25


def _bounded_document_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    separator = "\n\n[... bounded recall omitted middle text ...]\n\n"
    usable = max(1, limit - (2 * len(separator)))
    head = usable // 2
    middle = usable // 4
    tail = usable - head - middle
    midpoint = len(text) // 2
    middle_start = max(head, midpoint - (middle // 2))
    return separator.join(
        (
            text[:head],
            text[middle_start : middle_start + middle],
            text[-tail:] if tail else "",
        )
    )[:limit]


def _public_analysis(source: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    document = analysis.get("document") if isinstance(analysis.get("document"), dict) else {}
    public_document = {key: value for key, value in document.items() if key not in {"path", "text"}}
    return {
        "file_id": source["file_id"],
        "name": source["name"],
        "summary": analysis.get("summary"),
        "text_preview": analysis.get("text_preview"),
        "document": public_document,
        "signals": analysis.get("signals") or {},
        "tables": list(analysis.get("tables") or [])[:12],
        "formulas": list(analysis.get("formulas") or [])[:40],
        "recommendations": list(analysis.get("recommendations") or [])[:12],
    }


def _public_corpus(
    corpus: dict[str, Any],
    sources: list[dict[str, Any]],
    paths: list[Path],
) -> dict[str, Any]:
    source_by_path = {
        str(path.resolve(strict=False)): source for source, path in zip(sources, paths, strict=True)
    }
    files = []
    for item in corpus.get("files") or []:
        if not isinstance(item, dict):
            continue
        source = source_by_path.get(str(Path(str(item.get("path") or "")).resolve(strict=False)))
        files.append(
            {
                "file_id": source.get("file_id") if source else None,
                "name": item.get("name"),
                "kind": item.get("kind"),
                "chars": item.get("chars"),
                "headings": list(item.get("headings") or [])[:12],
                "focus_hits": list(item.get("focus_hits") or [])[:8],
                "entities": list(item.get("entities") or [])[:16],
            }
        )
    return {
        "summary": corpus.get("summary") or {},
        "files": files,
        "combined_outline": str(corpus.get("combined_outline") or "")[:8_000],
        "errors": list(corpus.get("errors") or [])[:12],
    }


def _retrieval_query(query: str) -> str:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\w-]+", query, flags=re.UNICODE):
        normalized = token.casefold().strip("._-")
        if (
            len(normalized) < 2
            or normalized in _RECALL_STOPWORDS
            or any(normalized.startswith(stem) for stem in _RECALL_NOISE_STEMS)
        ):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(token)
        if len(terms) >= 12:
            break
    return " ".join(terms)


def _allows_recent_fallback(query: str, retrieval_query: str) -> bool:
    normalized = query.casefold()
    if retrieval_query:
        return False
    return any(marker in normalized for marker in (*_RECENT_MARKERS, *_MEMORY_MARKERS))


def _document_recency_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("created_at") or ""),
        str(record.get("updated_at") or ""),
        str(record.get("id") or ""),
    )


def _requests_recent(query: str) -> bool:
    normalized = query.casefold()
    return any(marker in normalized for marker in _RECENT_MARKERS)


def _requests_multiple(query: str) -> bool:
    normalized = query.casefold()
    return any(marker in normalized for marker in _MULTI_MARKERS)
