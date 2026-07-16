from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import parse_qsl, urlparse

T = TypeVar("T")
WebOperation = Literal[
    "search_requests",
    "fetches",
    "renders",
    "network_bytes",
    "content_chars",
]


class WebMode(StrEnum):
    FAST_FACT = "FAST_FACT"
    DEEP_RESEARCH = "DEEP_RESEARCH"
    AGGRESSIVE_SHOPPING = "AGGRESSIVE_SHOPPING"


class WebBudgetExceeded(RuntimeError):
    """Raised when a web run reaches its shared deadline or operation budget."""


@dataclass(frozen=True, slots=True)
class WebBudgetLimits:
    deadline_sec: float
    search_requests: int
    fetches: int
    renders: int
    network_bytes: int
    content_chars: int
    max_sources: int
    concurrency: int


MODE_LIMITS: Mapping[WebMode, WebBudgetLimits] = {
    WebMode.FAST_FACT: WebBudgetLimits(
        deadline_sec=5.0,
        # 4 lets an independent fallback engine (Mojeek) be tried after the three
        # mainstream engines; `stop_when` short-circuits on the first success, so a
        # healthy DuckDuckGo still costs a single request.
        search_requests=4,
        fetches=0,
        renders=0,
        network_bytes=1_000_000,
        content_chars=60_000,
        max_sources=6,
        concurrency=3,
    ),
    WebMode.DEEP_RESEARCH: WebBudgetLimits(
        deadline_sec=60.0,
        search_requests=12,
        fetches=12,
        renders=4,
        network_bytes=6_000_000,
        content_chars=300_000,
        max_sources=10,
        concurrency=4,
    ),
    WebMode.AGGRESSIVE_SHOPPING: WebBudgetLimits(
        deadline_sec=90.0,
        search_requests=15,
        fetches=14,
        renders=10,
        network_bytes=10_000_000,
        content_chars=500_000,
        max_sources=12,
        concurrency=3,
    ),
}


@dataclass(frozen=True, slots=True)
class WebRequest:
    query: str
    mode: WebMode
    region: str = "ru-ru"
    freshness: str = ""
    deadline_sec: float | None = None


@dataclass(frozen=True, slots=True)
class BoundedResult(Generic[T]):
    index: int
    value: T | None = None
    error: str = ""
    budget_exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


def normalize_web_mode(value: Any, *, default: WebMode) -> WebMode:
    if isinstance(value, WebMode):
        return value
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "": default,
        "FAST": WebMode.FAST_FACT,
        "FACT": WebMode.FAST_FACT,
        "FAST_FACT": WebMode.FAST_FACT,
        "DEEP": WebMode.DEEP_RESEARCH,
        "RESEARCH": WebMode.DEEP_RESEARCH,
        "DEEP_RESEARCH": WebMode.DEEP_RESEARCH,
        "SHOP": WebMode.AGGRESSIVE_SHOPPING,
        "SHOPPING": WebMode.AGGRESSIVE_SHOPPING,
        "AGGRESSIVE": WebMode.AGGRESSIVE_SHOPPING,
        "AGGRESSIVE_SHOPPING": WebMode.AGGRESSIVE_SHOPPING,
    }
    mode = aliases.get(raw)
    if mode is None:
        supported = ", ".join(item.value for item in WebMode)
        raise ValueError(f"Unsupported web mode {value!r}; expected one of: {supported}.")
    return mode


class WebRunBudget:
    def __init__(self, mode: WebMode, *, deadline_sec: float | None = None) -> None:
        base = MODE_LIMITS[mode]
        requested = base.deadline_sec if deadline_sec is None else float(deadline_sec)
        if requested <= 0:
            raise ValueError("deadline_sec must be positive.")
        # A caller may tighten a mode deadline, but cannot silently turn a bounded
        # request into an unbounded browser job.
        effective_deadline = min(requested, base.deadline_sec)
        self.mode = mode
        self.limits = WebBudgetLimits(
            deadline_sec=effective_deadline,
            search_requests=base.search_requests,
            fetches=base.fetches,
            renders=base.renders,
            network_bytes=base.network_bytes,
            content_chars=base.content_chars,
            max_sources=base.max_sources,
            concurrency=base.concurrency,
        )
        self._started_at = time.monotonic()
        self._deadline_at = self._started_at + effective_deadline
        self._consumed: dict[WebOperation, int] = {
            "search_requests": 0,
            "fetches": 0,
            "renders": 0,
            "network_bytes": 0,
            "content_chars": 0,
        }
        self._lock = asyncio.Lock()
        self._warnings: list[str] = []

    def remaining_sec(self) -> float:
        return max(0.0, self._deadline_at - time.monotonic())

    def expired(self) -> bool:
        return self.remaining_sec() <= 0

    async def reserve(self, operation: WebOperation, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("Budget reservation cannot be negative.")
        async with self._lock:
            if self.expired():
                raise WebBudgetExceeded("Web run deadline exhausted.")
            limit = int(getattr(self.limits, operation))
            consumed = self._consumed[operation]
            if consumed + amount > limit:
                raise WebBudgetExceeded(
                    f"Web {operation} budget exhausted ({consumed}/{limit})."
                )
            self._consumed[operation] = consumed + amount

    async def run(
        self,
        operation: WebOperation,
        factory: Callable[[], Awaitable[T]],
        *,
        amount: int = 1,
    ) -> T:
        await self.reserve(operation, amount)
        remaining = self.remaining_sec()
        if remaining <= 0:
            raise WebBudgetExceeded("Web run deadline exhausted.")
        try:
            return await asyncio.wait_for(factory(), timeout=remaining)
        except TimeoutError as exc:
            raise WebBudgetExceeded("Web run deadline exhausted during I/O.") from exc

    async def account_content(self, value: str) -> None:
        await self.reserve("content_chars", len(value))

    def warn(self, warning: str) -> None:
        compact = " ".join(str(warning).split())
        if compact and compact not in self._warnings:
            self._warnings.append(compact[:300])

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(0.0, time.monotonic() - self._started_at)
        return {
            "deadline_sec": round(self.limits.deadline_sec, 3),
            "elapsed_sec": round(elapsed, 3),
            "remaining_sec": round(self.remaining_sec(), 3),
            "limits": {
                "search_requests": self.limits.search_requests,
                "fetches": self.limits.fetches,
                "renders": self.limits.renders,
                "network_bytes": self.limits.network_bytes,
                "content_chars": self.limits.content_chars,
                "max_sources": self.limits.max_sources,
                "concurrency": self.limits.concurrency,
            },
            "consumed": dict(self._consumed),
            "deadline_exhausted": self.expired(),
            "warnings": list(self._warnings),
        }


@dataclass(slots=True)
class WebOrchestrator:
    request: WebRequest
    budget: WebRunBudget
    _metadata: dict[str, Any] = field(default_factory=dict)
    _io_semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._io_semaphore = asyncio.Semaphore(self.budget.limits.concurrency)

    @classmethod
    def create(
        cls,
        *,
        query: str,
        mode: WebMode,
        region: str = "ru-ru",
        freshness: str = "",
        deadline_sec: float | None = None,
    ) -> WebOrchestrator:
        request = WebRequest(
            query=query,
            mode=mode,
            region=region,
            freshness=freshness,
            deadline_sec=deadline_sec,
        )
        return cls(request=request, budget=WebRunBudget(mode, deadline_sec=deadline_sec))

    @property
    def mode(self) -> WebMode:
        return self.request.mode

    @property
    def limits(self) -> WebBudgetLimits:
        return self.budget.limits

    async def run(
        self,
        operation: WebOperation,
        factory: Callable[[], Awaitable[T]],
        *,
        amount: int = 1,
    ) -> T:
        async with self._io_semaphore:
            return await self.budget.run(operation, factory, amount=amount)

    async def bounded_map(
        self,
        items: Sequence[T],
        worker: Callable[[T], Awaitable[Any]],
        *,
        concurrency: int | None = None,
        stop_when: Callable[[BoundedResult[Any]], bool] | None = None,
    ) -> list[BoundedResult[Any]]:
        limit = max(1, min(concurrency or self.limits.concurrency, self.limits.concurrency))
        semaphore = asyncio.Semaphore(limit)

        async def run_one(index: int, item: T) -> BoundedResult[Any]:
            async with semaphore:
                if self.budget.expired():
                    return BoundedResult(
                        index=index,
                        error="Web run deadline exhausted.",
                        budget_exhausted=True,
                    )
                try:
                    value = await worker(item)
                except WebBudgetExceeded as exc:
                    self.budget.warn(str(exc))
                    return BoundedResult(
                        index=index,
                        error=str(exc),
                        budget_exhausted=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    return BoundedResult(index=index, error=str(exc))
                return BoundedResult(index=index, value=value)

        tasks = [asyncio.create_task(run_one(index, item)) for index, item in enumerate(items)]
        if not tasks:
            return []
        try:
            if stop_when is None:
                return list(await asyncio.gather(*tasks))
            completed: list[BoundedResult[Any]] = []
            for future in asyncio.as_completed(tasks):
                result = await future
                completed.append(result)
                if stop_when(result):
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break
            return sorted(completed, key=lambda item: item.index)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def metadata(self) -> dict[str, Any]:
        semantics = {
            WebMode.FAST_FACT: "snippet-first hedged search without browser rendering",
            WebMode.DEEP_RESEARCH: "bounded parallel source retrieval and cross-verification",
            WebMode.AGGRESSIVE_SHOPPING: (
                "dynamic rendering, offer extraction, sponsored-SEO filtering, and "
                "negative technical review analysis"
            ),
        }
        return {
            "mode": self.mode.value,
            "semantics": semantics[self.mode],
            "budget": self.budget.snapshot(),
            **self._metadata,
        }

    def enrich_shopping_sources(
        self,
        sources: Sequence[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        issue_domains: dict[str, set[str]] = {}
        offers: list[dict[str, str]] = []
        negative_reviews: list[dict[str, Any]] = []
        for source in sources:
            enriched = dict(source)
            analysis = analyze_shopping_source(enriched)
            enriched.pop("_shopping_analysis_text", None)
            enriched["shopping_analysis"] = analysis
            url = str(enriched.get("url") or "")
            domain = _domain(url)
            for offer in analysis["offers"]:
                offers.append({**offer, "url": url, "domain": domain})
            for review in analysis["negative_technical_reviews"]:
                negative_reviews.append({**review, "url": url, "domain": domain})
                issue_domains.setdefault(str(review["category"]), set()).add(domain)
            if analysis["excluded"]:
                filtered.append(
                    {
                        "url": url,
                        "domain": domain,
                        "reasons": analysis["exclusion_reasons"],
                    }
                )
                continue
            accepted.append(enriched)

        corroborated = [
            {"category": category, "domains": sorted(domains), "source_count": len(domains)}
            for category, domains in sorted(issue_domains.items())
            if len(domains) >= 2
        ]
        accepted.sort(
            key=lambda item: (
                int((item.get("shopping_analysis") or {}).get("source_score") or 0),
                float(item.get("answer_score") or 0),
            ),
            reverse=True,
        )
        summary = {
            "offers": _deduplicate_offers(offers)[:30],
            "negative_technical_reviews": negative_reviews[:30],
            "corroborated_technical_issues": corroborated,
            "filtered_paid_seo_count": sum(
                1 for item in filtered if "sponsored_seo" in item["reasons"]
            ),
            "excluded_low_signal_count": sum(
                1 for item in filtered if "low_signal_snippet" in item["reasons"]
            ),
            "excluded_sources": filtered[:20],
        }
        self._metadata["shopping"] = summary
        return accepted, filtered, summary


def analyze_shopping_source(source: Mapping[str, Any]) -> dict[str, Any]:
    url = str(source.get("url") or "")
    domain = _domain(url)
    text = _source_text(source)
    normalized = text.casefold()
    query_pairs = {key.casefold(): value for key, value in parse_qsl(urlparse(url).query)}
    sponsor_markers = _matches_any(normalized, _SPONSORED_PATTERNS)
    affiliate_params = sorted(
        key
        for key in query_pairs
        if key.startswith("utm_") or key in {"aff", "affiliate", "affid", "ref", "tag"}
    )
    seo_markers = _matches_any(normalized, _SEO_PATTERNS)
    user_review_markers = _matches_any(normalized, _USER_REVIEW_PATTERNS)
    offers = _extract_offers(text)
    product_page = bool(
        re.search(r"/(?:product|products|item|catalog|p)/", urlparse(url).path, re.IGNORECASE)
    )
    community = any(
        marker in domain
        for marker in (
            "reddit.",
            "4pda.",
            "forum.",
            "forums.",
            "community.",
            "otzovik.",
            "irecommend.",
        )
    )
    reviews = _negative_technical_reviews(text)
    sponsored = bool(sponsor_markers or affiliate_params)
    review_signal = bool(user_review_markers or community or reviews)
    merchant_offer = bool(product_page and offers)
    paid_seo = bool(
        sponsored
        and len(seo_markers) >= 2
        and not review_signal
        and not merchant_offer
    )
    low_signal = bool(
        not source.get("fetched")
        and not offers
        and not review_signal
        and not source.get("price")
    )
    exclusion_reasons: list[str] = []
    if paid_seo:
        exclusion_reasons.append("sponsored_seo")
    if low_signal:
        exclusion_reasons.append("low_signal_snippet")
    source_score = 0
    source_score += 4 if review_signal else 0
    source_score += 3 if reviews else 0
    source_score += 2 if merchant_offer else 0
    source_score -= 3 if sponsored else 0
    source_score -= min(3, len(seo_markers))
    return {
        "domain": domain,
        "source_kind": (
            "community_review"
            if community
            else "merchant_offer"
            if merchant_offer
            else "review"
            if review_signal
            else "publisher"
        ),
        "sponsored": sponsored,
        "sponsor_markers": sponsor_markers,
        "affiliate_params": affiliate_params,
        "seo_markers": seo_markers,
        "review_signal": review_signal,
        "offers": offers,
        "negative_technical_reviews": reviews,
        "filtered_as_paid_seo": paid_seo,
        "excluded_low_signal": low_signal,
        "excluded": paid_seo or low_signal,
        "exclusion_reasons": exclusion_reasons,
        "source_score": source_score,
    }


_SPONSORED_PATTERNS = (
    r"\bsponsored\b",
    r"\bpromoted\b",
    r"\badvertorial\b",
    r"\baffiliate links?\b",
    r"\bpaid partnership\b",
    r"\bреклам[аы]\b",
    r"\bпартн[её]рск(?:ий|ая|ое) материал\b",
)
_SEO_PATTERNS = (
    r"\btop\s+\d+\b",
    r"\bbest .{0,40}\b20\d{2}\b",
    r"\bultimate buying guide\b",
    r"\bexclusive (?:deal|discount|coupon)\b",
    r"\bclick here to buy\b",
    r"\bтоп[- ]?\d+\b",
    r"\bлучши[ейх].{0,40}\b20\d{2}\b",
    r"\bпромокод\b",
)
_USER_REVIEW_PATTERNS = (
    r"\bverified purchase\b",
    r"\bowner review\b",
    r"\blong[- ]term review\b",
    r"\bpros and cons\b",
    r"\bнедостатк(?:и|ов)\b",
    r"\bопыт использования\b",
    r"\bподтвержд[её]нная покупка\b",
)
_NEGATIVE_PATTERNS = (
    r"\b(?:fails?|failed|failure|broken|crash(?:es|ed)?|freeze[sd]?|overheat(?:s|ed|ing)?|"
    r"drain(?:s|ed|ing)?|noisy|noise|unstable|disconnect(?:s|ed)?|dead pixels?)\b",
    r"\b(?:problem|issue|drawback|defect|complaint)s?\b",
    r"\b(?:сломал(?:ся|ась|ось)?|перегрева(?:ется|лся|лась)?|греется|шумит|трещит|"
    r"зависает|вылетает|отваливается|разряжается|брак|дефект|проблем[аы]|недостат(?:ок|ки))\b",
)
_TECHNICAL_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    "battery": ("battery", "charge", "drain", "аккумуля", "батаре", "заряд"),
    "thermal": ("overheat", "thermal", "temperature", "hot", "перегрев", "греется"),
    "stability": ("crash", "freeze", "unstable", "hang", "вылет", "завис"),
    "software": ("driver", "firmware", "bios", "update", "драйвер", "прошив", "обновлен"),
    "display": ("display", "screen", "pixel", "flicker", "экран", "пиксел", "мерц"),
    "connectivity": (
        "wifi",
        "wi-fi",
        "bluetooth",
        "disconnect",
        "network",
        "связ",
        "отваливается",
    ),
    "acoustics": ("noise", "noisy", "fan", "coil whine", "шум", "вентилят", "трещит"),
    "mechanical": ("hinge", "button", "keyboard", "port", "корпус", "кнопк", "разъ[её]м"),
}


def _source_text(source: Mapping[str, Any]) -> str:
    parts = [
        str(source.get(key) or "")
        for key in (
            "title",
            "snippet",
            "excerpt",
            "text",
            "price",
            "rating",
            "_shopping_analysis_text",
        )
    ]
    extraction = source.get("extraction")
    if isinstance(extraction, Mapping):
        for key in ("description", "summary", "pros", "cons", "reviews"):
            value = extraction.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, Iterable) and not isinstance(value, str | bytes | Mapping):
                parts.extend(str(item) for item in value)
    shopping_analysis = source.get("shopping_analysis")
    if isinstance(shopping_analysis, Mapping):
        for review in shopping_analysis.get("negative_technical_reviews") or []:
            if isinstance(review, Mapping):
                parts.append(str(review.get("excerpt") or ""))
        for offer in shopping_analysis.get("offers") or []:
            if isinstance(offer, Mapping):
                parts.append(str(offer.get("raw") or ""))
    return " ".join(" ".join(parts).split())[:30_000]


def _matches_any(text: str, patterns: Sequence[str]) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text, re.IGNORECASE)]


def _negative_technical_reviews(text: str) -> list[dict[str, str]]:
    reviews: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        compact = " ".join(sentence.split())
        if not 25 <= len(compact) <= 500:
            continue
        normalized = compact.casefold()
        if not any(re.search(pattern, normalized, re.IGNORECASE) for pattern in _NEGATIVE_PATTERNS):
            continue
        category = next(
            (
                name
                for name, markers in _TECHNICAL_CATEGORIES.items()
                if any(marker in normalized for marker in markers)
            ),
            "other_technical",
        )
        key = (category, normalized[:160])
        if key in seen:
            continue
        seen.add(key)
        reviews.append({"category": category, "excerpt": compact[:420]})
        if len(reviews) >= 8:
            break
    return reviews


def _extract_offers(text: str) -> list[dict[str, str]]:
    patterns = (
        re.compile(
            r"(?<!\w)(?P<currency>[$€£₽]|USD|EUR|GBP|RUB)\s*"
            r"(?P<amount>\d{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?<!\w)(?P<amount>\d{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*"
            r"(?P<currency>₽|руб(?:\.|лей)?|USD|EUR|GBP|RUB|доллар(?:ов|а)?|евро)",
            re.IGNORECASE,
        ),
    )
    offers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            amount = _decimal_amount(match.group("amount"))
            currency = _currency_code(match.group("currency"))
            if amount is None or not currency:
                continue
            normalized = format(amount.normalize(), "f")
            key = (normalized, currency)
            if key in seen:
                continue
            seen.add(key)
            offers.append(
                {
                    "amount": normalized,
                    "currency": currency,
                    "raw": " ".join(match.group(0).split())[:80],
                }
            )
            if len(offers) >= 12:
                return offers
    return offers


def _decimal_amount(raw: str) -> Decimal | None:
    compact = raw.replace(" ", "").replace("\u00a0", "")
    if "," in compact and "." in compact:
        decimal_separator = "," if compact.rfind(",") > compact.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        compact = compact.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif compact.count(",") == 1 and len(compact.rsplit(",", 1)[1]) <= 2:
        compact = compact.replace(",", ".")
    elif not (compact.count(".") == 1 and len(compact.rsplit(".", 1)[1]) <= 2):
        compact = compact.replace(",", "").replace(".", "")
    try:
        amount = Decimal(compact)
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _currency_code(raw: str) -> str:
    value = raw.strip().casefold().rstrip(".")
    if value in {"₽", "rub", "руб", "рублей"}:
        return "RUB"
    if value in {"$", "usd", "доллар", "доллара", "долларов"}:
        return "USD"
    if value in {"€", "eur", "евро"}:
        return "EUR"
    if value in {"£", "gbp"}:
        return "GBP"
    return ""


def _deduplicate_offers(offers: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for offer in offers:
        key = (
            str(offer.get("amount") or ""),
            str(offer.get("currency") or ""),
            str(offer.get("domain") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(offer))
    return result


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").casefold()
