"""Isolated, production-grade async web-surfing module for the Jarvis agent.

``JarvisWebSurfer`` is a self-contained black box: it accepts high-level
commands, autonomously gathers data of arbitrary complexity, and returns clean
structured results (dict / Markdown). It has no dependency on the rest of the
Jarvis codebase and can be dropped into any async Python 3.11+ project.

Four layers are implemented:

1. Stealth & transport
   - ``playwright-stealth`` integration (optional; degrades if absent).
   - Residential/datacenter proxy rotation (``http://user:pass@host:port``),
     custom headers, and User-Agent rotation.
   - Human pacing: randomized action delays, smooth cursor movement, and
     variable typing speed.

2. Interception & smart parsing
   - Direct XHR/Fetch response interception — if the site serves JSON, the
     surfer keeps the clean JSON and ignores the HTML.
   - Automatic extraction of app state from page globals (``__NEXT_DATA__``,
     ``window.__INITIAL_STATE__``, ``__NUXT__``, ``__APOLLO_STATE__``).
   - Shadow DOM and nested ``<iframe>`` traversal.

3. Self-healing & sanitization
   - Resilient selectors: CSS -> text markers -> XPath -> Schema.org/JSON-LD.
   - Aggressive HTML cleaning (drops script/style/svg/nav/footer/ads) into
     clean semantic Markdown.

4. Public interface (see ``JarvisWebSurfer``):
   - ``fast_fact`` — API-first search with an aggressive 2s budget.
   - ``deep_research`` — parallel crawl of top links with deduplication.
   - ``aggressive_shopping`` — product card + targeted negative-review mining.

Dependencies (install into the runtime):
    pip install playwright beautifulsoup4 lxml playwright-stealth
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

try:  # BeautifulSoup is required for sanitization.
    from bs4 import BeautifulSoup
    from bs4.element import Comment, Tag
except ImportError as _bs_exc:  # pragma: no cover - import guard
    raise ImportError(
        "web_surfer requires beautifulsoup4: pip install beautifulsoup4 lxml"
    ) from _bs_exc

try:  # Playwright is required for the browser layers.
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Response,
        async_playwright,
    )
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ImportError as _pw_exc:  # pragma: no cover - import guard
    raise ImportError(
        "web_surfer requires playwright: pip install playwright && "
        "playwright install chromium"
    ) from _pw_exc


__all__ = [
    "AntiBotError",
    "BrowserLaunchError",
    "JarvisWebSurfer",
    "NavigationError",
    "ParsingError",
    "ProxyError",
    "SurferConfig",
    "SurferTimeoutError",
    "WebSurferError",
    "shop_search_url",
]


def shop_search_url(shop: str | None, query: str) -> str:
    """Public helper: build a shop search URL, or '' if the shop is unknown."""

    return _shop_search_url(shop, query)

LOGGER = logging.getLogger("jarvis.web_surfer")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class WebSurferError(RuntimeError):
    """Base class for every error raised by the surfer."""


class BrowserLaunchError(WebSurferError):
    """Raised when the browser or Playwright driver cannot start."""


class ProxyError(WebSurferError):
    """Raised when every configured proxy fails or a proxy string is invalid."""


class NavigationError(WebSurferError):
    """Raised when a page cannot be navigated to or never becomes usable."""


class AntiBotError(WebSurferError):
    """Raised when a page is an anti-bot / CAPTCHA / access-denied wall."""


class ParsingError(WebSurferError):
    """Raised when required structured data cannot be parsed from a page."""


class SurferTimeoutError(WebSurferError):
    """Raised when an operation exceeds its time budget."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
_DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

_ANTIBOT_MARKERS: tuple[str, ...] = (
    "verify you are human",
    "are you a robot",
    "checking your browser",
    "just a moment",
    "enable javascript and cookies",
    "captcha",
    "cf-challenge",
    "cf-turnstile",
    "unusual traffic",
    "access denied",
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "проверка браузера",
)

# Tags and container hints that never carry primary content.
_NOISE_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "svg",
    "noscript",
    "template",
    "iframe",
    "form",
    "nav",
    "footer",
    "header",
    "aside",
    "button",
    "input",
    "select",
    "textarea",
)

_NOISE_HINTS: tuple[str, ...] = (
    "cookie",
    "consent",
    "banner",
    "advert",
    "adsbygoogle",
    "promo",
    "popup",
    "modal",
    "subscribe",
    "newsletter",
    "breadcrumb",
    "pagination",
    "sidebar",
    "menu",
    "navbar",
    "social",
    "share",
    "recommend",
    "related",
)


@dataclass
class SurferConfig:
    """Tunable behavior for :class:`JarvisWebSurfer`."""

    headless: bool = True
    proxies: list[str] = field(default_factory=list)
    user_agents: list[str] = field(default_factory=lambda: list(_DEFAULT_USER_AGENTS))
    extra_headers: dict[str, str] = field(default_factory=dict)
    locale: str = "ru-RU"
    timezone_id: str = "Europe/Moscow"
    viewport_width: int = 1366
    viewport_height: int = 900
    nav_timeout_ms: int = 30_000
    default_timeout_ms: int = 15_000
    fast_fact_budget_sec: float = 2.0
    deep_research_budget_sec: float = 45.0
    shopping_budget_sec: float = 60.0
    max_concurrency: int = 3
    min_action_delay_sec: float = 0.25
    max_action_delay_sec: float = 1.1
    min_type_delay_ms: float = 40.0
    max_type_delay_ms: float = 170.0
    max_chars_per_page: int = 20_000
    use_stealth: bool = True

    def __post_init__(self) -> None:
        if not self.user_agents:
            self.user_agents = list(_DEFAULT_USER_AGENTS)
        self.max_concurrency = max(1, min(8, int(self.max_concurrency)))


# --------------------------------------------------------------------------- #
# Optional stealth shim (tolerates multiple playwright-stealth versions)
# --------------------------------------------------------------------------- #
async def _apply_stealth(page: Page) -> bool:
    """Apply playwright-stealth to a page if the library is importable.

    Returns True when stealth was applied. Never raises: stealth is a best
    effort hardening, not a hard requirement.
    """

    try:
        module = __import__("playwright_stealth")
    except ImportError:
        return False
    # Newer API: Stealth().apply_stealth_async(page)
    stealth_cls = getattr(module, "Stealth", None)
    if stealth_cls is not None:
        with suppress(Exception):
            instance = stealth_cls()
            applier = getattr(instance, "apply_stealth_async", None)
            if applier is not None:
                await applier(page)
                return True
    # Older API: stealth_async(page)
    legacy = getattr(module, "stealth_async", None)
    if legacy is not None:
        with suppress(Exception):
            await legacy(page)
            return True
    return False


# --------------------------------------------------------------------------- #
# In-page JavaScript used across layers
# --------------------------------------------------------------------------- #
# Recursively read text from the light DOM, open shadow roots, and same-origin
# iframes. Cross-origin iframes are skipped silently (browser security).
_DEEP_TEXT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const push = (t) => { if (t && t.trim()) out.push(t.trim()); };
  const walk = (root) => {
    if (!root || seen.has(root)) return;
    seen.add(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const el of nodes) {
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  const frames = document.querySelectorAll("iframe");
  for (const frame of frames) {
    try {
      const doc = frame.contentDocument;
      if (doc && doc.body) push(doc.body.innerText);
    } catch (err) { /* cross-origin, ignore */ }
  }
  return { iframeText: out.join("\n\n") };
}
"""

# Pull well-known global app-state blobs.
_APP_STATE_JS = r"""
() => {
  const grab = (fn) => { try { return fn(); } catch (e) { return null; } };
  const nextEl = document.getElementById("__NEXT_DATA__");
  return {
    next_data: nextEl ? nextEl.textContent : null,
    initial_state: grab(() => JSON.stringify(window.__INITIAL_STATE__)),
    nuxt: grab(() => JSON.stringify(window.__NUXT__)),
    apollo: grab(() => JSON.stringify(window.__APOLLO_STATE__)),
    preloaded: grab(() => JSON.stringify(window.__PRELOADED_STATE__)),
  };
}
"""

# Open a shop's city selector and pick the first available preferred city.
# Receives the preferred city list as the evaluate() argument.
_SET_CITY_JS = r"""
(wanted) => {
  const norm = (v) => String(v || "").toLowerCase().replace(/ё/g, "е")
    .replace(/\s+/g, " ").trim();
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none"
      && Number(s.opacity || "1") > 0 && r.width > 0 && r.height > 0;
  };
  const txt = (el) => [
    el.innerText || el.textContent || "",
    el.getAttribute("aria-label") || "",
    el.getAttribute("title") || "",
    el.getAttribute("data-city") || "",
  ].join(" ");
  const openMarkers = [
    "ваш город", "выберите город", "выбрать город", "выбор города", "город:",
    "изменить город", "укажите город", "мой город", "город доставки",
    "select city", "choose city", "your city",
  ];
  const clickables = () => Array.from(document.querySelectorAll(
    "button, a, [role=button], [role=link], span, div, [data-city]"
  )).filter(vis);
  // Already set to a wanted city?
  for (const el of clickables()) {
    const t = norm(txt(el));
    if (!openMarkers.some((m) => t.includes(m))) continue;
    for (const w of wanted) if (t.includes(norm(w)))
      return { ok: true, city: norm(w), opened: false };
  }
  let opener = null;
  for (const el of clickables()) {
    const t = norm(txt(el));
    if (openMarkers.some((m) => t.includes(m))) { opener = el; break; }
  }
  if (!opener) return { ok: false, city: "", reason: "no_selector" };
  opener.scrollIntoView({ block: "center" });
  opener.click();
  return new Promise((resolve) => setTimeout(() => {
    for (const w of wanted) {
      const wn = norm(w);
      const opts = Array.from(document.querySelectorAll(
        "a, button, li, span, div, [role=option], [data-city]"
      )).filter(vis);
      let exact = null; let partial = null;
      for (const el of opts) {
        const t = norm(txt(el));
        if (!t) continue;
        if (t === wn) { exact = el; break; }
        if (!partial && t.length <= wn.length + 12 && t.includes(wn)) partial = el;
      }
      const pick = exact || partial;
      if (pick) { pick.scrollIntoView({ block: "center" }); pick.click();
        return resolve({ ok: true, city: wn, opened: true }); }
    }
    resolve({ ok: false, city: "", opened: true, reason: "city_not_offered" });
  }, 900));
}
"""

# Collect visible text from every open shadow root (concatenated).
_SHADOW_TEXT_JS = r"""
() => {
  const parts = [];
  const visit = (root) => {
    const all = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const el of all) {
      if (el.shadowRoot) {
        const t = el.shadowRoot.textContent;
        if (t && t.trim()) parts.push(t.trim());
        visit(el.shadowRoot);
      }
    }
  };
  visit(document);
  return parts.join("\n");
}
"""


# --------------------------------------------------------------------------- #
# Surfer
# --------------------------------------------------------------------------- #
class JarvisWebSurfer:
    """Autonomous async web surfer. Use as an async context manager.

    Example::

        async with JarvisWebSurfer(proxies=["http://user:pass@host:3128"]) as surfer:
            fact = await surfer.fast_fact("курс доллара сегодня")
            report = await surfer.deep_research("лучшие ssd 2 тб", max_depth=3)
            card = await surfer.aggressive_shopping("https://www.dns-shop.ru/product/...")
    """

    def __init__(
        self,
        config: SurferConfig | None = None,
        *,
        proxies: Sequence[str] | None = None,
        user_agents: Sequence[str] | None = None,
        headless: bool | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or SurferConfig()
        if proxies is not None:
            self.config.proxies = list(proxies)
        if user_agents is not None:
            self.config.user_agents = list(user_agents) or list(_DEFAULT_USER_AGENTS)
        if headless is not None:
            self.config.headless = headless
        self.log = logger or LOGGER
        self._playwright: Any = None
        self._browser: Browser | None = None
        self._proxy_index = 0
        self._ua_index = 0
        self._started = False

    # ----------------------------- lifecycle ------------------------------ #
    async def __aenter__(self) -> JarvisWebSurfer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Launch Playwright and the Chromium browser."""

        if self._started:
            return
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.config.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        except (PlaywrightError, OSError) as exc:
            await self._safe_stop_playwright()
            raise BrowserLaunchError(f"Could not launch Chromium: {exc}") from exc
        self._started = True

    async def close(self) -> None:
        """Close the browser and stop the Playwright driver."""

        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        await self._safe_stop_playwright()
        self._started = False

    async def _safe_stop_playwright(self) -> None:
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    def _ensure_started(self) -> Browser:
        if not self._started or self._browser is None:
            raise WebSurferError("Surfer is not started; use 'async with' or call start().")
        return self._browser

    # --------------------------- transport layer -------------------------- #
    def _next_proxy(self) -> dict[str, str] | None:
        proxies = self.config.proxies
        if not proxies:
            return None
        raw = proxies[self._proxy_index % len(proxies)]
        self._proxy_index += 1
        return _parse_proxy(raw)

    def _next_user_agent(self) -> str:
        agents = self.config.user_agents
        ua = agents[self._ua_index % len(agents)]
        self._ua_index += 1
        return ua

    async def _new_context(self, *, rotate_proxy: bool = True) -> BrowserContext:
        browser = self._ensure_started()
        proxy = self._next_proxy() if rotate_proxy else None
        headers = {
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            **self.config.extra_headers,
        }
        try:
            context = await browser.new_context(
                user_agent=self._next_user_agent(),
                locale=self.config.locale,
                timezone_id=self.config.timezone_id,
                viewport={
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
                proxy=proxy,  # type: ignore[arg-type]
                extra_http_headers=headers,
                ignore_https_errors=True,
            )
        except (PlaywrightError, ValueError) as exc:
            if proxy is not None:
                raise ProxyError(f"Proxy context failed: {exc}") from exc
            raise BrowserLaunchError(f"Could not create browser context: {exc}") from exc
        context.set_default_timeout(self.config.default_timeout_ms)
        context.set_default_navigation_timeout(self.config.nav_timeout_ms)
        return context

    async def _new_page(self, context: BrowserContext) -> Page:
        page = await context.new_page()
        if self.config.use_stealth:
            await _apply_stealth(page)
        return page

    # ---------------------------- human pacing ---------------------------- #
    async def _human_pause(self, scale: float = 1.0) -> None:
        low = self.config.min_action_delay_sec * scale
        high = self.config.max_action_delay_sec * scale
        await asyncio.sleep(random.uniform(low, max(low, high)))

    async def _human_mouse_move(self, page: Page) -> None:
        with suppress(PlaywrightError):
            width = self.config.viewport_width
            height = self.config.viewport_height
            for _ in range(random.randint(2, 4)):
                x = random.randint(int(width * 0.1), int(width * 0.9))
                y = random.randint(int(height * 0.1), int(height * 0.9))
                await page.mouse.move(x, y, steps=random.randint(6, 18))
                await asyncio.sleep(random.uniform(0.05, 0.2))

    async def _human_type(self, page: Page, selector: str, text: str) -> bool:
        try:
            handle = await page.query_selector(selector)
            if handle is None:
                return False
            await handle.scroll_into_view_if_needed(timeout=self.config.default_timeout_ms)
            await handle.click()
            for char in text:
                await handle.type(char, delay=random.uniform(
                    self.config.min_type_delay_ms, self.config.max_type_delay_ms
                ))
            return True
        except (PlaywrightError, PlaywrightTimeoutError):
            return False

    # ------------------------- navigation + guards ------------------------ #
    async def _goto(
        self,
        page: Page,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
    ) -> Response | None:
        try:
            response = await page.goto(
                url,
                wait_until=wait_until,  # type: ignore[arg-type]
                timeout=self.config.nav_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"Navigation timed out: {url}") from exc
        except PlaywrightError as exc:
            raise NavigationError(f"Navigation failed for {url}: {exc}") from exc
        await self._human_pause(0.6)
        return response

    async def _guard_antibot(self, page: Page) -> None:
        with suppress(PlaywrightError):
            content = (await page.content())[:6000].lower()
            title = (await page.title()).lower()
            haystack = f"{title} {content}"
            if any(marker in haystack for marker in _ANTIBOT_MARKERS):
                raise AntiBotError(f"Anti-bot / CAPTCHA wall detected on {page.url}")

    # --------------------------- interception ----------------------------- #
    @staticmethod
    def _attach_interceptors(page: Page, sink: list[dict[str, Any]]) -> None:
        """Collect JSON XHR/Fetch responses into ``sink`` (bounded)."""

        async def _on_response(response: Response) -> None:
            if len(sink) >= 40:
                return
            try:
                request = response.request
                if request.resource_type not in {"xhr", "fetch"}:
                    return
                content_type = (response.headers or {}).get("content-type", "")
                if "json" not in content_type.lower():
                    return
                body = await response.json()
            except Exception:  # noqa: BLE001 - never let interception break a page
                return
            sink.append({"url": response.url, "status": response.status, "json": body})

        page.on("response", lambda response: asyncio.ensure_future(_on_response(response)))

    async def _extract_app_state(self, page: Page) -> dict[str, Any]:
        state: dict[str, Any] = {}
        with suppress(PlaywrightError):
            raw = await page.evaluate(_APP_STATE_JS)
            for key, value in (raw or {}).items():
                if not value:
                    continue
                parsed = _safe_json_loads(value)
                if parsed is not None:
                    state[key] = parsed
        return state

    async def _deep_text(self, page: Page) -> str:
        chunks: list[str] = []
        with suppress(PlaywrightError):
            shadow = await page.evaluate(_SHADOW_TEXT_JS)
            if isinstance(shadow, str) and shadow.strip():
                chunks.append(shadow.strip())
        with suppress(PlaywrightError):
            frames = await page.evaluate(_DEEP_TEXT_JS)
            iframe_text = (frames or {}).get("iframeText") if isinstance(frames, dict) else None
            if isinstance(iframe_text, str) and iframe_text.strip():
                chunks.append(iframe_text.strip())
        return "\n\n".join(chunks)

    # ----------------------- sanitization / markdown ---------------------- #
    def _clean_html_to_markdown(self, html: str, *, base_url: str = "") -> str:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001 - lxml optional, fall back to builtin parser
            soup = BeautifulSoup(html, "html.parser")

        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()
        for tag_name in _NOISE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        for tag in list(soup.find_all(True)):
            if not isinstance(tag, Tag):
                continue
            hint = " ".join(
                [
                    str(tag.get("id") or ""),
                    " ".join(tag.get("class") or []),
                    str(tag.get("role") or ""),
                ]
            ).lower()
            if hint and any(noise in hint for noise in _NOISE_HINTS):
                tag.decompose()

        root = soup.body or soup
        lines: list[str] = []
        for element in root.descendants:
            if not isinstance(element, Tag):
                continue
            name = element.name
            if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                text = _collapse(element.get_text(" ", strip=True))
                if text:
                    lines.append(f"{'#' * int(name[1])} {text}")
            elif name == "li":
                text = _collapse(element.get_text(" ", strip=True))
                if text:
                    lines.append(f"- {text}")
            elif name == "a":
                text = _collapse(element.get_text(" ", strip=True))
                href = element.get("href") or ""
                if text and href and not href.startswith("javascript"):
                    absolute = urljoin(base_url, href) if base_url else href
                    lines.append(f"[{text}]({absolute})")
            elif name == "p":
                text = _collapse(element.get_text(" ", strip=True))
                if text:
                    lines.append(text)
        markdown = _dedupe_lines(lines)
        return markdown[: self.config.max_chars_per_page]

    @staticmethod
    def _extract_jsonld(html: str) -> list[dict[str, Any]]:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:  # noqa: BLE001
            return []
        blocks: list[dict[str, Any]] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            parsed = _safe_json_loads(script.string or "")
            if isinstance(parsed, dict):
                blocks.append(parsed)
            elif isinstance(parsed, list):
                blocks.extend(item for item in parsed if isinstance(item, dict))
        return blocks

    # ------------------------ resilient extraction ------------------------ #
    async def _find_text(
        self,
        page: Page,
        *,
        css_selectors: Iterable[str] = (),
        text_markers: Iterable[str] = (),
        xpath_selectors: Iterable[str] = (),
    ) -> str:
        """Return the first non-empty text via CSS -> XPath -> text markers."""

        for selector in css_selectors:
            with suppress(PlaywrightError, PlaywrightTimeoutError):
                handle = await page.query_selector(selector)
                if handle is not None:
                    text = _collapse(await handle.inner_text())
                    if text:
                        return text
        for xpath in xpath_selectors:
            with suppress(PlaywrightError, PlaywrightTimeoutError):
                handle = await page.query_selector(f"xpath={xpath}")
                if handle is not None:
                    text = _collapse(await handle.inner_text())
                    if text:
                        return text
        for marker in text_markers:
            with suppress(PlaywrightError, PlaywrightTimeoutError):
                handle = await page.query_selector(f"text=/{re.escape(marker)}/i")
                if handle is not None:
                    text = _collapse(await handle.inner_text())
                    if text:
                        return text
        return ""

    # =============================== PUBLIC =============================== #
    async def fast_fact(self, query: str) -> dict[str, Any]:
        """Fast API-first search with an aggressive time budget.

        Returns::

            {
                "ok": bool,
                "query": str,
                "answer": str,          # DuckDuckGo instant answer if any
                "snippets": [{"title": str, "url": str, "snippet": str}],
                "source": "duckduckgo",
                "elapsed_ms": int,
                "error": str | None,
            }
        """

        query = " ".join(str(query or "").split())
        started = time.perf_counter()
        if not query:
            return _fast_fact_result(query, ok=False, error="empty query", started=started)
        try:
            return await asyncio.wait_for(
                self._fast_fact_impl(query, started),
                timeout=self.config.fast_fact_budget_sec,
            )
        except TimeoutError:
            return _fast_fact_result(
                query, ok=False, error="fast_fact exceeded budget", started=started
            )
        except WebSurferError as exc:
            return _fast_fact_result(query, ok=False, error=str(exc), started=started)

    async def _fast_fact_impl(self, query: str, started: float) -> dict[str, Any]:
        api_ctx = await self._playwright.request.new_context(
            extra_http_headers={"User-Agent": self._next_user_agent()},
            proxy=self._next_proxy(),  # type: ignore[arg-type]
        )
        try:
            answer = ""
            snippets: list[dict[str, str]] = []
            with suppress(Exception):
                ddg = await api_ctx.get(
                    "https://api.duckduckgo.com/"
                    f"?q={quote_plus(query)}&format=json&no_html=1&no_redirect=1",
                    timeout=self.config.fast_fact_budget_sec * 1000,
                )
                if ddg.ok:
                    data = await ddg.json()
                    answer = str(
                        data.get("AbstractText")
                        or data.get("Answer")
                        or data.get("Definition")
                        or ""
                    ).strip()
                    snippets.extend(_ddg_related_snippets(data))
            if len(snippets) < 3:
                with suppress(Exception):
                    html_resp = await api_ctx.get(
                        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                        timeout=self.config.fast_fact_budget_sec * 1000,
                    )
                    if html_resp.ok:
                        body = await html_resp.text()
                        snippets.extend(_parse_ddg_html(body))
            deduped = _dedupe_snippets(snippets)[:8]
            return _fast_fact_result(
                query,
                ok=bool(answer or deduped),
                answer=answer,
                snippets=deduped,
                started=started,
            )
        finally:
            with suppress(Exception):
                await api_ctx.dispose()

    async def deep_research(self, query: str, max_depth: int = 3) -> str:
        """Parallel crawl of the top search links; returns a deduplicated Markdown report.

        ``max_depth`` bounds how many top links are fetched (1..8).
        """

        query = " ".join(str(query or "").split())
        if not query:
            return "# Исследование\n\nПустой запрос."
        depth = max(1, min(8, int(max_depth)))
        try:
            return await asyncio.wait_for(
                self._deep_research_impl(query, depth),
                timeout=self.config.deep_research_budget_sec,
            )
        except TimeoutError:
            return f"# Исследование: {query}\n\nПревышен бюджет времени на обход источников."
        except WebSurferError as exc:
            return f"# Исследование: {query}\n\nНе удалось собрать источники: {exc}"

    async def _deep_research_impl(self, query: str, depth: int) -> str:
        seed = await self.fast_fact(query)
        links: list[str] = []
        for snippet in seed.get("snippets", []):
            url = str(snippet.get("url") or "")
            if url.startswith("http") and url not in links:
                links.append(url)
        links = links[:depth]
        if not links:
            answer = str(seed.get("answer") or "").strip()
            if answer:
                return f"# Исследование: {query}\n\n{answer}\n"
            return f"# Исследование: {query}\n\nНе нашёл источников в открытой выдаче."

        semaphore = asyncio.Semaphore(self.config.max_concurrency)

        async def _fetch(url: str) -> dict[str, str] | None:
            async with semaphore:
                return await self._research_one(url)

        gathered = await asyncio.gather(*(_fetch(url) for url in links), return_exceptions=True)
        sections: list[dict[str, str]] = [
            item for item in gathered if isinstance(item, dict) and item.get("markdown")
        ]
        return _compose_research_report(query, seed.get("answer", ""), sections)

    async def _research_one(self, url: str) -> dict[str, str] | None:
        context = await self._new_context()
        try:
            page = await self._new_page(context)
            await self._goto(page, url)
            with suppress(AntiBotError):
                await self._guard_antibot(page)
            await self._human_mouse_move(page)
            html = ""
            with suppress(PlaywrightError):
                html = await page.content()
            markdown = self._clean_html_to_markdown(html, base_url=url) if html else ""
            title = ""
            with suppress(PlaywrightError):
                title = _collapse(await page.title())
            if not markdown:
                return None
            return {"url": url, "title": title or url, "markdown": markdown}
        except NavigationError:
            return None
        finally:
            with suppress(Exception):
                await context.close()

    async def shop_search(
        self,
        query: str,
        *,
        shop: str | None = None,
        search_url: str | None = None,
        max_items: int = 24,
        cities: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Render a shop's search page and return products ranked cheapest-first.

        This is the "найди самую дешёвую X на <магазин>" capability: a real
        browser renders the JS/anti-bot catalog that httpx cannot read, product
        tiles are extracted (title + price + url), and the list is sorted by
        price ascending. Before reading, it tries to set the delivery city from
        ``cities`` (default Донецк -> Москва) so regional prices are correct.

        Returns::

            {
                "ok": bool, "query": str, "shop": str, "url": str, "city": str,
                "count": int,
                "cheapest": {title,url,price_text,price_value} | None,
                "items": [{title,url,price_text,price_value,in_stock}],
                "error": str | None,
            }
        """

        query = " ".join(str(query or "").split())
        url = (search_url or "").strip() or _shop_search_url(shop, query)
        if not query:
            return _shop_search_result(query, shop, ok=False, error="empty query")
        if not url:
            return _shop_search_result(
                query, shop, ok=False, error=f"unknown shop: {shop!r} (pass search_url)"
            )
        want_cities = list(cities) if cities else ["Донецк", "Москва"]
        try:
            return await asyncio.wait_for(
                self._shop_search_impl(query, shop or "", url, max_items, want_cities),
                timeout=self.config.shopping_budget_sec,
            )
        except TimeoutError:
            return _shop_search_result(
                query, shop, url=url, ok=False, error="shop_search exceeded budget"
            )
        except AntiBotError as exc:
            return _shop_search_result(query, shop, url=url, ok=False, error=f"anti-bot: {exc}")
        except WebSurferError as exc:
            return _shop_search_result(query, shop, url=url, ok=False, error=str(exc))

    async def _shop_search_impl(
        self,
        query: str,
        shop: str,
        url: str,
        max_items: int,
        cities: list[str],
    ) -> dict[str, Any]:
        context = await self._new_context()
        try:
            page = await self._new_page(context)
            await self._goto(page, url, wait_until="domcontentloaded")
            await self._guard_antibot(page)
            await self._human_mouse_move(page)
            city = await self._try_set_city(page, cities)
            # Let the JS grid and lazy prices settle, then nudge lazy-load.
            with suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=9_000)
            for _ in range(3):
                with suppress(PlaywrightError):
                    await page.mouse.wheel(0, 2200)
                await self._human_pause(0.5)
            html = ""
            with suppress(PlaywrightError):
                html = await page.content()
            items = _extract_catalog_items(html, base_url=url)
            ranked = _rank_catalog_items(items)[:max_items]
            priced = [item for item in ranked if item.get("price_value") is not None]
            return _shop_search_result(
                query,
                shop,
                url=url,
                city=city,
                ok=bool(ranked),
                items=ranked,
                cheapest=priced[0] if priced else None,
            )
        finally:
            with suppress(Exception):
                await context.close()

    async def _try_set_city(self, page: Page, cities: Sequence[str]) -> str:
        """Best-effort delivery-city selection; returns the chosen city or ''."""

        wanted = [str(c).strip() for c in cities if str(c).strip()]
        if not wanted:
            return ""
        with suppress(PlaywrightError):
            result = await page.evaluate(_SET_CITY_JS, wanted)
            if isinstance(result, dict) and result.get("ok"):
                await self._human_pause(1.0)
                with suppress(PlaywrightError):
                    await page.wait_for_load_state("networkidle", timeout=6_000)
                return str(result.get("city") or "")
        return ""

    async def aggressive_shopping(self, product_url: str) -> dict[str, Any]:
        """Render a product card, extract price/specs/availability, and mine real
        negative reviews (1-3 stars, "Минусы/Недостатки"), skipping 5-star spam.

        Returns::

            {
                "ok": bool,
                "url": str,
                "title": str,
                "price": {"text": str, "value": float | None, "currency": str},
                "availability": str,
                "specs": {name: value},
                "rating": {"value": float | None, "count": int | None},
                "negative_reviews": [
                    {"rating": int | None, "text": str, "cons": str}
                ],
                "captured_api": [str],   # interception URLs that returned JSON
                "error": str | None,
            }
        """

        product_url = str(product_url or "").strip()
        if not product_url.startswith("http"):
            return _shopping_result(product_url, ok=False, error="invalid product URL")
        try:
            return await asyncio.wait_for(
                self._aggressive_shopping_impl(product_url),
                timeout=self.config.shopping_budget_sec,
            )
        except TimeoutError:
            return _shopping_result(product_url, ok=False, error="shopping exceeded budget")
        except AntiBotError as exc:
            return _shopping_result(product_url, ok=False, error=f"anti-bot: {exc}")
        except WebSurferError as exc:
            return _shopping_result(product_url, ok=False, error=str(exc))

    async def _aggressive_shopping_impl(self, product_url: str) -> dict[str, Any]:
        context = await self._new_context()
        api_sink: list[dict[str, Any]] = []
        try:
            page = await self._new_page(context)
            self._attach_interceptors(page, api_sink)
            await self._goto(page, product_url, wait_until="domcontentloaded")
            await self._guard_antibot(page)
            await self._human_mouse_move(page)
            with suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=8_000)

            html = ""
            with suppress(PlaywrightError):
                html = await page.content()
            jsonld = self._extract_jsonld(html)
            app_state = await self._extract_app_state(page)

            title = await self._find_text(
                page,
                css_selectors=(
                    "h1[itemprop=name]",
                    "[data-product-name]",
                    "h1.product-title",
                    "h1",
                ),
                xpath_selectors=("//h1",),
            )
            price = await self._extract_price(page, jsonld)
            availability = await self._extract_availability(page, jsonld)
            specs = await self._extract_specs(page)
            rating = _extract_rating(jsonld)

            negative = await self._mine_negative_reviews(page)

            title = title or _jsonld_field(jsonld, "name") or ""
            return _shopping_result(
                product_url,
                ok=bool(title or price.get("text") or negative),
                title=title,
                price=price,
                availability=availability,
                specs=specs,
                rating=rating,
                negative_reviews=negative,
                captured_api=[item["url"] for item in api_sink][:10],
                app_state_keys=sorted(app_state.keys()),
            )
        finally:
            with suppress(Exception):
                await context.close()

    # --------------------- shopping sub-extractors ------------------------ #
    async def _extract_price(self, page: Page, jsonld: list[dict[str, Any]]) -> dict[str, Any]:
        text = await self._find_text(
            page,
            css_selectors=(
                "[itemprop=price]",
                "[data-price-type=finalPrice]",
                "[class*=product-buy__price]",
                "[class*=price__current]",
                "[class*=price-current]",
                "[class*=price] [class*=value]",
            ),
            text_markers=("₽", "руб"),
            xpath_selectors=("//*[contains(@class,'price')][contains(text(),'₽')]",),
        )
        ld_price = _jsonld_offer_field(jsonld, "price")
        currency = _jsonld_offer_field(jsonld, "priceCurrency") or ("RUB" if "₽" in text else "")
        value = _parse_amount(text) or _parse_amount(ld_price)
        display = text or (f"{ld_price} {currency}".strip() if ld_price else "")
        return {"text": display, "value": value, "currency": currency or "RUB"}

    async def _extract_availability(self, page: Page, jsonld: list[dict[str, Any]]) -> str:
        text = await self._find_text(
            page,
            css_selectors=(
                "[itemprop=availability]",
                "[class*=availability]",
                "[class*=order-avail]",
                "[class*=product-buy__availability]",
            ),
            text_markers=("в наличии", "нет в наличии", "под заказ", "ожидается"),
        )
        if text:
            return text
        ld = _jsonld_offer_field(jsonld, "availability")
        if ld:
            return "в наличии" if "InStock" in str(ld) else str(ld)
        return ""

    async def _extract_specs(self, page: Page) -> dict[str, str]:
        specs: dict[str, str] = {}
        selectors = (
            "[class*=characteristics] tr",
            "[class*=product-characteristics] li",
            "[class*=spec] tr",
            "table.specs tr",
            "dl [class*=spec]",
        )
        for selector in selectors:
            with suppress(PlaywrightError):
                rows = await page.query_selector_all(selector)
                for row in rows[:80]:
                    with suppress(PlaywrightError):
                        cells = await row.query_selector_all("td, th, dt, dd, span, div")
                        texts = []
                        for cell in cells[:4]:
                            texts.append(_collapse(await cell.inner_text()))
                        texts = [item for item in texts if item]
                        if len(texts) >= 2 and texts[0] != texts[1]:
                            key = texts[0][:80]
                            if key and key not in specs:
                                specs[key] = texts[1][:200]
                if specs:
                    break
        return specs

    async def _mine_negative_reviews(self, page: Page) -> list[dict[str, Any]]:
        """Navigate to reviews, target 1-3 stars / cons, extract negative feedback."""

        collected: list[dict[str, Any]] = []
        # Try to open a reviews tab/section first.
        for marker in ("Отзывы", "отзыв", "Reviews"):
            with suppress(PlaywrightError, PlaywrightTimeoutError):
                handle = await page.query_selector(f"text=/{marker}/i")
                if handle is not None:
                    await handle.scroll_into_view_if_needed(timeout=6_000)
                    await handle.click()
                    await self._human_pause(0.8)
                    break

        # Prefer a "worst first" / low-rating filter if one is present.
        for marker in (
            "Сначала отрицательные",
            "С низкой оценкой",
            "Сначала негативные",
            "Худшие",
            "1 звезда",
        ):
            with suppress(PlaywrightError, PlaywrightTimeoutError):
                handle = await page.query_selector(f"text=/{re.escape(marker)}/i")
                if handle is not None:
                    await handle.click()
                    await self._human_pause(0.8)
                    break

        # Paginate / lazy-load a few pages of reviews.
        for _ in range(6):
            collected.extend(await self._read_review_blocks(page))
            if len({item["text"] for item in collected}) >= 25:
                break
            advanced = False
            for marker in ("Показать ещё", "Загрузить ещё", "Ещё отзывы", "Следующая", "Далее"):
                with suppress(PlaywrightError, PlaywrightTimeoutError):
                    handle = await page.query_selector(f"text=/{re.escape(marker)}/i")
                    if handle is not None:
                        await handle.scroll_into_view_if_needed(timeout=6_000)
                        await handle.click()
                        await self._human_pause(1.0)
                        advanced = True
                        break
            if not advanced:
                with suppress(PlaywrightError):
                    await page.mouse.wheel(0, 2400)
                    await self._human_pause(0.7)
        return _filter_negative_reviews(collected)

    async def _read_review_blocks(self, page: Page) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        selectors = (
            "[itemprop=review]",
            "[class*=review-card]",
            "[class*=product-reviews__item]",
            "[class*=comment-item]",
            "[data-review-id]",
            "[class*=review]",
        )
        elements: list[Any] = []
        for selector in selectors:
            with suppress(PlaywrightError):
                elements = await page.query_selector_all(selector)
                if elements:
                    break
        for element in elements[:60]:
            with suppress(PlaywrightError):
                raw = _collapse(await element.inner_text())
                if not raw or len(raw) < 8:
                    continue
                rating = await _review_rating(element)
                cons = _extract_cons(raw)
                blocks.append({"rating": rating, "text": raw[:1500], "cons": cons})
        return blocks


# --------------------------------------------------------------------------- #
# Module-level helpers (pure functions)
# --------------------------------------------------------------------------- #
def _parse_proxy(raw: str) -> dict[str, str]:
    raw = str(raw or "").strip()
    if not raw:
        raise ProxyError("Empty proxy string.")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ProxyError(f"Invalid proxy string: {raw}")
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    proxy: dict[str, str] = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None


def _collapse(text: str | None) -> str:
    return " ".join(str(text or "").split())


def _dedupe_lines(lines: Sequence[str]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = re.sub(r"\s+", " ", line.strip().lower())[:160]
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(line.strip())
    return "\n\n".join(output)


def _dedupe_snippets(snippets: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for item in snippets:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        key = url or title.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "title": title[:200],
                "url": url,
                "snippet": str(item.get("snippet") or "").strip()[:400],
            }
        )
    return output


def _ddg_related_snippets(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for topic in data.get("RelatedTopics", []) or []:
        if not isinstance(topic, dict):
            continue
        if "Topics" in topic:
            for sub in topic.get("Topics", []) or []:
                if isinstance(sub, dict) and sub.get("FirstURL"):
                    out.append(
                        {
                            "title": _collapse(sub.get("Text", ""))[:120],
                            "url": str(sub.get("FirstURL")),
                            "snippet": _collapse(sub.get("Text", "")),
                        }
                    )
        elif topic.get("FirstURL"):
            out.append(
                {
                    "title": _collapse(topic.get("Text", ""))[:120],
                    "url": str(topic.get("FirstURL")),
                    "snippet": _collapse(topic.get("Text", "")),
                }
            )
    return out


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*\bresult__a\b[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(?P<snippet>.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)


def _parse_ddg_html(html: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    snippets = _DDG_SNIPPET_RE.findall(html)
    for index, match in enumerate(_DDG_RESULT_RE.finditer(html)):
        href = unescape(match.group("href"))
        url = _unwrap_ddg_url(href)
        title = _collapse(_strip_tags(match.group("title")))
        if not url.startswith("http") or not title:
            continue
        snippet = ""
        if index < len(snippets):
            snippet = _collapse(_strip_tags(snippets[index]))
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= 12:
            break
    return results


def _unwrap_ddg_url(href: str) -> str:
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    if href.startswith("//"):
        return f"https:{href}"
    return href


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _compose_research_report(
    query: str,
    answer: str,
    sections: Sequence[dict[str, str]],
) -> str:
    parts: list[str] = [f"# Исследование: {query}", ""]
    if answer:
        parts.append(f"**Кратко:** {answer}")
        parts.append("")
    if not sections:
        parts.append("Источники не дали читаемого текста.")
        return "\n".join(parts)
    for section in sections:
        parts.append(f"## {section['title']}")
        parts.append(section["url"])
        parts.append("")
        parts.append(section["markdown"])
        parts.append("")
    return "\n".join(parts).strip()


def _parse_amount(raw: Any) -> float | None:
    text = str(raw or "")
    if not text:
        return None
    match = re.search(r"\d[\d\s.,]{1,}", text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    return value if 1 <= value <= 1_000_000_000 else None


def _jsonld_field(blocks: Sequence[dict[str, Any]], field_name: str) -> str:
    for block in blocks:
        if str(block.get("@type") or "").lower() in {"product", "offer", "aggregateoffer"}:
            value = block.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _jsonld_offer_field(blocks: Sequence[dict[str, Any]], field_name: str) -> str:
    for block in blocks:
        offers = block.get("offers")
        candidates: list[dict[str, Any]] = []
        if isinstance(offers, dict):
            candidates.append(offers)
        elif isinstance(offers, list):
            candidates.extend(item for item in offers if isinstance(item, dict))
        for offer in candidates:
            value = offer.get(field_name)
            if value not in (None, ""):
                return str(value)
    return ""


def _extract_rating(blocks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    for block in blocks:
        aggregate = block.get("aggregateRating")
        if isinstance(aggregate, dict):
            value = _parse_amount(aggregate.get("ratingValue"))
            count = aggregate.get("reviewCount") or aggregate.get("ratingCount")
            try:
                count_int = int(re.sub(r"[^\d]", "", str(count))) if count else None
            except ValueError:
                count_int = None
            return {"value": value, "count": count_int}
    return {"value": None, "count": None}


async def _review_rating(element: Any) -> int | None:
    """Best-effort per-review star rating from aria-label, data attrs, or width."""

    for attribute in ("data-rating", "data-mark", "data-grade", "aria-label", "title"):
        with suppress(Exception):
            raw = await element.get_attribute(attribute)
            if raw:
                match = re.search(r"([1-5])(?:[.,]0)?\s*(?:звёзд|звезд|star|из 5|/5)?", str(raw))
                if match:
                    return int(match.group(1))
    # Star-fill width heuristic (e.g. width:60% -> 3 stars).
    with suppress(Exception):
        filled = await element.query_selector("[style*=width]")
        if filled is not None:
            style = await filled.get_attribute("style") or ""
            width = re.search(r"width:\s*(\d{1,3})%", style)
            if width:
                return max(1, min(5, round(int(width.group(1)) / 20)))
    return None


_CONS_RE = re.compile(
    r"(?:минусы|недостатки|cons)\s*[:\-—]?\s*(?P<body>.+?)"
    r"(?:комментарий|достоинства|плюсы|$)",
    flags=re.IGNORECASE | re.DOTALL,
)


def _extract_cons(text: str) -> str:
    match = _CONS_RE.search(text)
    if not match:
        return ""
    return _collapse(match.group("body"))[:600]


def _filter_negative_reviews(reviews: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep 1-3 star reviews and any with explicit cons; drop 5-star spam."""

    negative: list[dict[str, Any]] = []
    seen: set[str] = set()
    for review in reviews:
        rating = review.get("rating")
        cons = str(review.get("cons") or "").strip()
        text = str(review.get("text") or "").strip()
        is_low = isinstance(rating, int) and rating <= 3
        # If rating is unknown, keep it only when it carries an explicit "cons" block.
        keep = is_low or (rating is None and bool(cons))
        if not keep:
            continue
        key = (cons or text)[:120].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        negative.append(
            {
                "rating": rating,
                "text": text[:1000],
                "cons": cons,
            }
        )
        if len(negative) >= 20:
            break
    return negative


def _fast_fact_result(
    query: str,
    *,
    ok: bool,
    answer: str = "",
    snippets: list[dict[str, str]] | None = None,
    error: str | None = None,
    started: float,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "query": query,
        "answer": answer,
        "snippets": snippets or [],
        "source": "duckduckgo",
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
    }


# Search-URL templates for major RU shops/marketplaces. {q} is url-encoded.
_SHOP_SEARCH_TEMPLATES: dict[str, str] = {
    "dns": "https://www.dns-shop.ru/search/?q={q}&order=price&stock=all",
    "citilink": "https://www.citilink.ru/search/?text={q}",
    "mvideo": "https://www.mvideo.ru/product-list-page?q={q}",
    "eldorado": "https://www.eldorado.ru/search/catalog.php?q={q}",
    "ozon": "https://www.ozon.ru/search/?text={q}&sorting=price",
    "wildberries": "https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
    "yandex market": "https://market.yandex.ru/search?text={q}",
    "regard": "https://www.regard.ru/catalog?search={q}",
}

# Shop name aliases (RU/EN) -> canonical key above.
_SHOP_ALIASES: dict[str, str] = {
    "dns": "dns",
    "днс": "dns",
    "dns-shop": "dns",
    "dns-shop.ru": "dns",
    "ситилинк": "citilink",
    "citilink": "citilink",
    "мвидео": "mvideo",
    "m.видео": "mvideo",
    "mvideo": "mvideo",
    "эльдорадо": "eldorado",
    "eldorado": "eldorado",
    "озон": "ozon",
    "ozon": "ozon",
    "вайлдберриз": "wildberries",
    "вб": "wildberries",
    "wildberries": "wildberries",
    "wb": "wildberries",
    "яндекс маркет": "yandex market",
    "яндекс.маркет": "yandex market",
    "yandex market": "yandex market",
    "регард": "regard",
    "regard": "regard",
}


def _normalize_shop(shop: str | None) -> str:
    key = str(shop or "").strip().lower().replace("ё", "е")
    return _SHOP_ALIASES.get(key, key)


def _shop_search_url(shop: str | None, query: str) -> str:
    template = _SHOP_SEARCH_TEMPLATES.get(_normalize_shop(shop))
    if not template:
        return ""
    return template.format(q=quote_plus(query))


_CATALOG_PRICE_RE = re.compile(
    r"(?P<num>\d[\d\s ]{2,})\s*(?:₽|руб(?:\.|лей|ля)?|р\.)"
    r"|(?:₽|руб)\s*(?P<num2>\d[\d\s ]{2,})",
    flags=re.IGNORECASE,
)
_PRODUCT_HREF_RE = re.compile(
    r"/(?:product|catalog|goods|p|item|detail|dp|context/detail)/", re.IGNORECASE
)


def _extract_catalog_items(html: str, *, base_url: str = "") -> list[dict[str, Any]]:
    """Extract product tiles (title, url, price) from a rendered catalog page.

    Tries Schema.org ``ItemList``/``Product`` first, then an anchor-first
    heuristic: any product-looking link whose tile also contains a RUB price.
    """

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - lxml optional
        soup = BeautifulSoup(html, "html.parser")

    items = _catalog_from_jsonld(soup, base_url)
    if len(items) >= 2:
        return items

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        title = _collapse(anchor.get_text(" ", strip=True))
        href = str(anchor.get("href") or "")
        if len(title) < 8 or not _PRODUCT_HREF_RE.search(href):
            continue
        url = urljoin(base_url, href) if base_url else href
        key = url.split("?", 1)[0]
        if key in seen:
            continue
        price_text = _nearest_price(anchor)
        if not price_text:
            continue
        seen.add(key)
        results.append(
            {
                "title": title[:200],
                "url": url,
                "price_text": price_text,
                "price_value": _parse_amount(price_text),
                "in_stock": None,
            }
        )
    return results


def _catalog_from_jsonld(soup: Any, base_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        parsed = _safe_json_loads(script.string or "")
        blocks = parsed if isinstance(parsed, list) else [parsed]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            elements = block.get("itemListElement")
            if not isinstance(elements, list):
                continue
            for element in elements:
                node = element.get("item") if isinstance(element, dict) else None
                node = node if isinstance(node, dict) else element
                if not isinstance(node, dict):
                    continue
                name = str(node.get("name") or "").strip()
                url = str(node.get("url") or element.get("url") or "").strip()
                if not name:
                    continue
                price = _jsonld_offer_field([node], "price") or _jsonld_field([node], "price")
                items.append(
                    {
                        "title": name[:200],
                        "url": urljoin(base_url, url) if (base_url and url) else url,
                        "price_text": f"{price} ₽" if price else "",
                        "price_value": _parse_amount(price),
                        "in_stock": None,
                    }
                )
    return items


def _nearest_price(anchor: Tag) -> str:
    node: Any = anchor
    for _ in range(6):
        node = node.parent
        if node is None or not isinstance(node, Tag):
            break
        match = _CATALOG_PRICE_RE.search(node.get_text(" ", strip=True))
        if match:
            raw = match.group("num") or match.group("num2") or ""
            return f"{_collapse(raw)} ₽"
    return ""


def _rank_catalog_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(item: dict[str, Any]) -> tuple[int, float]:
        value = item.get("price_value")
        if value is None:
            return (1, 0.0)
        return (0, float(value))

    return sorted(items, key=_key)


def _shop_search_result(
    query: str,
    shop: str | None,
    *,
    ok: bool,
    url: str = "",
    city: str = "",
    items: list[dict[str, Any]] | None = None,
    cheapest: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    rows = items or []
    return {
        "ok": ok,
        "query": query,
        "shop": _normalize_shop(shop) or (shop or ""),
        "url": url,
        "city": city,
        "count": len(rows),
        "cheapest": cheapest,
        "items": rows,
        "error": error,
    }


def _shopping_result(
    url: str,
    *,
    ok: bool,
    title: str = "",
    price: dict[str, Any] | None = None,
    availability: str = "",
    specs: dict[str, str] | None = None,
    rating: dict[str, Any] | None = None,
    negative_reviews: list[dict[str, Any]] | None = None,
    captured_api: list[str] | None = None,
    app_state_keys: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "url": url,
        "title": title,
        "price": price or {"text": "", "value": None, "currency": "RUB"},
        "availability": availability,
        "specs": specs or {},
        "rating": rating or {"value": None, "count": None},
        "negative_reviews": negative_reviews or [],
        "captured_api": captured_api or [],
        "app_state_keys": app_state_keys or [],
        "error": error,
    }
