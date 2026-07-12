"""Durable document recall over Jarvis file storage and document_surfer."""

from __future__ import annotations

import re
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
    ) -> dict[str, Any]:
        query_clean = " ".join(str(query or "").split()).strip()
        if not query_clean and not file_ids:
            raise ValueError("query or file_ids is required")
        max_files = max(1, min(8, int(max_files)))
        max_chars = max(1_000, min(80_000, int(max_chars)))
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

    def _select_candidates(
        self,
        query: str,
        *,
        retrieval_query: str,
        file_ids: list[str],
        limit: int,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        errors: list[dict[str, Any]] = []
        if file_ids:
            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw_file_id in file_ids[:limit]:
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
        if retrieval_query:
            for record in self.storage.search_files(retrieval_query, limit=limit * 3):
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

        identity_terms = _identity_terms(retrieval_query)
        candidates = [
            _with_identity_match(item, identity_terms)
            for item in sorted(
                ranked.values(),
                key=lambda item: -float(item.get("match_score") or 0.0),
            )
            if _record_is_supported_document(item)
            and _passes_identity_threshold(item, identity_terms)
        ][: max(limit + 1, 2)]
        if candidates:
            if _requests_recent(query) and not _requests_multiple(query):
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
        if not normalized or _is_generic_document_term(normalized):
            continue
        if normalized not in terms:
            terms.append(normalized)
    return terms


def _is_generic_document_term(term: str) -> bool:
    return term in _GENERIC_DOCUMENT_TERMS or any(
        term.startswith(stem) for stem in _GENERIC_DOCUMENT_STEMS
    )


def _identity_matches(record: dict[str, Any], identity_terms: list[str]) -> list[str]:
    matched = {str(term).casefold() for term in record.get("matched_terms") or []}
    return [term for term in identity_terms if term in matched]


def _passes_identity_threshold(
    record: dict[str, Any],
    identity_terms: list[str],
) -> bool:
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
