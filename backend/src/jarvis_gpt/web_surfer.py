"""Isolated, production-grade async web-surfing module for the Jarvis agent.

``JarvisWebSurfer`` is an isolated browser black box: it accepts high-level
commands, autonomously gathers data of arbitrary complexity, and returns clean
structured results (dict / Markdown). Marketplace names and endpoints come from
Jarvis' small shared shop registry so routing and browser behavior cannot drift.

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
import math
import random
import re
import sys
import time
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse

from .shop_registry import get_shop_source, get_shop_source_by_host
from .shop_registry import shop_search_url as registry_shop_search_url

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


def shop_search_url(
    shop: str | None,
    query: str,
    *,
    criterion: str = "price_asc",
) -> str:
    """Public helper: build a shop search URL, or '' if the shop is unknown."""

    return _shop_search_url(shop, query, criterion=criterion)

LOGGER = logging.getLogger("jarvis.web_surfer")
_CLEANUP_TIMEOUT_SEC = 0.5
_PERSISTENT_CONTEXT_CLEANUP_TIMEOUT_SEC = 1.5


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
    # Some anti-bot providers deliberately reject Chromium's headless shell even
    # when the page is otherwise public.  On a Windows workstation we can retry
    # an empty/blocked catalog in the installed stable Chrome, headful but placed
    # off-screen.  The retry is never attempted in the Linux container runtime.
    headful_shop_fallback: bool = True
    headful_browser_channel: str = "chrome"
    shop_storage_state_dir: str = ""
    shop_persistent_profile_dir: str = ""

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
                await asyncio.wait_for(
                    self._browser.close(), timeout=_CLEANUP_TIMEOUT_SEC
                )
            self._browser = None
        await self._safe_stop_playwright()
        self._started = False

    async def _safe_stop_playwright(self) -> None:
        if self._playwright is not None:
            with suppress(Exception):
                await asyncio.wait_for(
                    self._playwright.stop(), timeout=_CLEANUP_TIMEOUT_SEC
                )
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

    async def _new_context(
        self,
        *,
        rotate_proxy: bool = True,
        browser: Browser | None = None,
        use_config_user_agent: bool = True,
        storage_state_path: Path | None = None,
    ) -> BrowserContext:
        browser = browser or self._ensure_started()
        proxy = self._next_proxy() if rotate_proxy else None
        headers = {
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            **self.config.extra_headers,
        }
        context_kwargs: dict[str, Any] = {
            "locale": self.config.locale,
            "timezone_id": self.config.timezone_id,
            "viewport": {
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            "proxy": proxy,
            "extra_http_headers": headers,
            "ignore_https_errors": True,
        }
        if use_config_user_agent:
            context_kwargs["user_agent"] = self._next_user_agent()
        if storage_state_path is not None and storage_state_path.is_file():
            context_kwargs["storage_state"] = str(storage_state_path)
        try:
            context = await browser.new_context(**context_kwargs)
        except (PlaywrightError, ValueError) as exc:
            if "storage_state" in context_kwargs:
                context_kwargs.pop("storage_state", None)
                try:
                    context = await browser.new_context(**context_kwargs)
                except (PlaywrightError, ValueError):
                    pass
                else:
                    context.set_default_timeout(self.config.default_timeout_ms)
                    context.set_default_navigation_timeout(self.config.nav_timeout_ms)
                    return context
            if proxy is not None:
                raise ProxyError(f"Proxy context failed: {exc}") from exc
            raise BrowserLaunchError(f"Could not create browser context: {exc}") from exc
        context.set_default_timeout(self.config.default_timeout_ms)
        context.set_default_navigation_timeout(self.config.nav_timeout_ms)
        return context

    def _shop_storage_state_path(self, shop: str | None) -> Path | None:
        root = str(self.config.shop_storage_state_dir or "").strip()
        source = get_shop_source(shop)
        if not root or source is None:
            return None
        safe_name = re.sub(r"[^a-z0-9_.-]+", "_", source.key.casefold()).strip("._")
        return Path(root) / f"{safe_name or 'shop'}.json"

    def _shop_persistent_profile_path(self, shop: str | None) -> Path | None:
        root = str(self.config.shop_persistent_profile_dir or "").strip()
        source = get_shop_source(shop)
        if not root or source is None:
            return None
        safe_name = re.sub(r"[^a-z0-9_.-]+", "_", source.key.casefold()).strip("._")
        return Path(root) / (safe_name or "shop")

    async def _persist_shop_storage_state(
        self,
        context: BrowserContext,
        shop: str | None,
    ) -> None:
        path = self._shop_storage_state_path(shop)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.wait_for(
                context.storage_state(path=str(path)), timeout=_CLEANUP_TIMEOUT_SEC
            )
        except (AttributeError, OSError, PlaywrightError, TimeoutError, TypeError):
            return

    async def _new_page(self, context: BrowserContext, *, use_stealth: bool | None = None) -> Page:
        page = await context.new_page()
        should_use_stealth = self.config.use_stealth if use_stealth is None else use_stealth
        if should_use_stealth:
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
        navigation = asyncio.create_task(
            page.goto(
                url,
                wait_until=wait_until,  # type: ignore[arg-type]
                timeout=self.config.nav_timeout_ms,
            )
        )
        try:
            response = await navigation
        except asyncio.CancelledError:
            navigation.cancel()
            await asyncio.gather(navigation, return_exceptions=True)
            raise
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

        # Drop navigation link-farms (menus, converter/cross-rate lists, related
        # grids): a block of many short anchors dominated by link text is chrome,
        # not content — dumping it verbatim buries the answer. Substantial-text
        # blocks and data tables are kept.
        for container in soup.find_all(["ul", "ol", "div", "section"]):
            if not isinstance(container, Tag):
                continue
            anchors = container.find_all("a")
            if len(anchors) < 10:
                continue
            total = _collapse(container.get_text(" ", strip=True))
            link_text = _collapse(
                " ".join(anchor.get_text(" ", strip=True) for anchor in anchors)
            )
            avg_anchor = len(link_text) / max(1, len(anchors))
            if total and len(link_text) >= 0.7 * len(total) and avg_anchor < 30:
                container.decompose()

        root = soup.body or soup
        # Plain-text safety net captured AFTER link-farm pruning: excludes nav and
        # cross-rate blocks but still recovers real content the structural walk below
        # might miss — the result is never empty and never a URL dump.
        fallback_text = _collapse(root.get_text(" ", strip=True))
        lines: list[str] = []
        # Tables carry the primary data on many reference pages (rates, prices,
        # schedules). Extract their rows first, then remove the tables so the block
        # walk below does not re-emit the same cells.
        for table in list(root.find_all("table")):
            for row in table.find_all("tr"):
                cells = [
                    _collapse(cell.get_text(" ", strip=True))
                    for cell in row.find_all(["td", "th"])
                ]
                cells = [cell for cell in cells if cell]
                if cells:
                    lines.append(" | ".join(cells))
            table.decompose()

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
                # Skip bare navigation items: a short link with no surrounding prose.
                if text and not (element.find("a") is not None and len(text) < 40):
                    lines.append(f"- {text}")
            elif name == "p":
                text = _collapse(element.get_text(" ", strip=True))
                if text:
                    lines.append(text)
        markdown = _dedupe_lines(lines)
        # If structural extraction yielded almost nothing (data hidden in link-heavy
        # markup that got pruned), fall back to the page's plain text — never empty,
        # never a URL dump.
        if len(markdown) < 200 and len(fallback_text) > len(markdown):
            markdown = fallback_text
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
        seed_snippets = list(seed.get("snippets", []))
        if not seed_snippets:
            # DuckDuckGo returned nothing (rate-limit/outage): fall back to the
            # independent provider so research does not dead-end on one search engine.
            with suppress(Exception):
                seed_snippets = await self.mojeek_search(query, limit=max(depth, 8))
        links: list[str] = []
        for snippet in seed_snippets:
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
        return _compose_research_report(
            query, seed.get("answer", ""), sections, seed_snippets
        )

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
                await asyncio.wait_for(context.close(), timeout=_CLEANUP_TIMEOUT_SEC)

    async def mojeek_search(self, query: str, *, limit: int = 8) -> list[dict[str, str]]:
        """Independent keyless fallback search via Mojeek, in a real browser page.

        Mojeek serves a captcha to lightweight HTTP clients (its API-context path is
        blocked) but returns a full result page to a genuine browser, so this reuses
        the surfer's anti-bot navigation. Used when DuckDuckGo is rate-limited or
        empty, so a single provider outage no longer blanks out web search.
        """

        query = " ".join(str(query or "").split())
        if not query:
            return []
        url = f"https://www.mojeek.com/search?q={quote_plus(query)}"
        context = await self._new_context()
        try:
            page = await self._new_page(context)
            await self._goto(page, url)
            with suppress(AntiBotError):
                await self._guard_antibot(page)
            html = ""
            with suppress(PlaywrightError):
                html = await page.content()
            return _parse_mojeek_html(html)[: max(1, limit)] if html else []
        except NavigationError:
            return []
        finally:
            with suppress(Exception):
                await asyncio.wait_for(context.close(), timeout=_CLEANUP_TIMEOUT_SEC)

    async def shop_search(
        self,
        query: str,
        *,
        shop: str | None = None,
        search_url: str | None = None,
        max_items: int = 24,
        cities: Sequence[str] | None = None,
        criterion: str = "price_asc",
        criterion_label: str = "",
        constraints: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Render a shop catalog and rank products by a requested criterion.

        Price comparisons use catalog prices. Other criteria (power, speed,
        capacity, range, runtime, size, rating, recency) retain typed units and
        only name a winner when comparable seller-declared metrics exist.

        Returns::

            {
                "ok": bool, "query": str, "shop": str, "url": str, "city": str,
                "count": int,
                "cheapest": {title,url,price_text,price_value} | None,
                "best": product | None, "comparison": {...},
                "items": [{title,url,price_text,price_value,in_stock}],
                "error": str | None,
            }
        """

        query = " ".join(str(query or "").split())
        criterion = _normalize_catalog_criterion(criterion)
        constraints = _normalize_catalog_constraints(constraints)
        neutral_query = _catalog_search_query(query, criterion)
        search_variants = _catalog_search_variants(query, criterion)
        search_query = search_variants[0] if search_variants else neutral_query
        url = (search_url or "").strip() or _shop_search_url(
            shop,
            search_query,
            criterion=criterion,
        )
        if not query:
            return _shop_search_result(
                query,
                shop,
                ok=False,
                error="empty query",
                criterion=criterion,
                criterion_label=criterion_label,
            )
        if criterion == "price_nearest" and constraints.get("target_price", 0) <= 0:
            return _shop_search_result(
                query,
                shop,
                ok=False,
                error="price_nearest requires a positive target_price constraint",
                criterion=criterion,
                criterion_label=criterion_label,
                constraints=constraints,
            )
        if not url:
            return _shop_search_result(
                query,
                shop,
                ok=False,
                error=f"unknown shop: {shop!r} (pass search_url)",
                criterion=criterion,
                criterion_label=criterion_label,
            )
        api_fallback: dict[str, Any] | None = None
        wildberries_api_enabled = (
            _normalize_shop(shop) == "wildberries" and not search_url and not cities
        )

        async def fetch_wildberries_api(timeout: float) -> dict[str, Any]:
            return await asyncio.wait_for(
                self._wildberries_api_shop_search(
                    query=query,
                    search_query=neutral_query,
                    max_items=max_items,
                    criterion=criterion,
                    criterion_label=criterion_label,
                    constraints=constraints,
                ),
                timeout=timeout,
            )

        async def retry_api_for_incomplete(
            current: dict[str, Any],
        ) -> dict[str, Any]:
            nonlocal api_fallback
            comparison = current.get("comparison") or {}
            if (
                not wildberries_api_enabled
                or criterion in {"price_asc", "price_desc", "price_nearest"}
                or comparison.get("complete")
            ):
                return current
            try:
                refreshed = await fetch_wildberries_api(
                    min(10.0, self.config.shopping_budget_sec * 0.2)
                )
            except (TimeoutError, PlaywrightError, WebSurferError, ValueError):
                return current
            refreshed_comparison = refreshed.get("comparison") or {}
            if refreshed.get("items"):
                api_fallback = refreshed
            return refreshed if refreshed_comparison.get("complete") else current

        if wildberries_api_enabled:
            try:
                api_result = await fetch_wildberries_api(
                    min(12.0, self.config.shopping_budget_sec * 0.25)
                )
                comparison = api_result.get("comparison") or {}
                if api_result.get("items"):
                    api_fallback = api_result
                if api_result.get("items") and (
                    criterion in {"price_asc", "price_desc", "price_nearest"}
                    or comparison.get("complete")
                ):
                    return api_result
            except (TimeoutError, PlaywrightError, WebSurferError, ValueError):
                pass
        want_cities = list(cities or [])
        started = time.monotonic()
        stages: list[dict[str, Any]] = []

        def finish(value: dict[str, Any]) -> dict[str, Any]:
            output = dict(value)
            output["stages"] = stages
            output["timings"] = {
                "total_ms": max(0, round((time.monotonic() - started) * 1000)),
                **{
                    f"{stage['name']}_ms": stage["elapsed_ms"]
                    for stage in stages
                    if stage.get("name") and isinstance(stage.get("elapsed_ms"), int)
                },
            }
            return output

        primary_error = ""
        can_retry_headful = (
            self.config.headless
            and self.config.headful_shop_fallback
            and sys.platform == "win32"
            and self._playwright is not None
        )
        stable_first = can_retry_headful and _normalize_shop(shop) == "dns"
        if stable_first:
            stage_started = time.monotonic()
            stable_error = ""
            stable_error_code = ""
            try:
                stable_result = await asyncio.wait_for(
                    self._shop_search_headful_chrome(
                        query,
                        shop or "",
                        url,
                        max_items,
                        want_cities,
                        criterion,
                        criterion_label,
                        search_query,
                        constraints,
                    ),
                    timeout=self.config.shopping_budget_sec,
                )
                stable_ok = bool(stable_result.get("ok") and stable_result.get("items"))
                stable_error = str(stable_result.get("error") or "no products parsed")
                stable_error_code = "empty_catalog" if not stable_ok else ""
            except TimeoutError:
                stable_result = None
                stable_ok = False
                stable_error = "stable Chrome shop search exceeded total budget"
                stable_error_code = "stable_timeout"
            except BrowserLaunchError as exc:
                stable_result = None
                stable_ok = False
                stable_error = f"stable Chrome unavailable: {exc}"
                stable_error_code = "stable_unavailable"
            except NavigationError as exc:
                stable_result = None
                stable_ok = False
                stable_error = f"stable Chrome navigation failed: {exc}"
                stable_error_code = "stable_navigation"
            except (PlaywrightError, OSError, WebSurferError) as exc:
                stable_result = None
                stable_ok = False
                stable_error = f"stable Chrome failed: {exc}"
                stable_error_code = "stable_failure"
            stages.append(
                {
                    "name": "stable_chrome",
                    "ok": stable_ok,
                    "elapsed_ms": max(0, round((time.monotonic() - stage_started) * 1000)),
                    "error_code": stable_error_code or None,
                    "error": stable_error if not stable_ok else None,
                }
            )
            if stable_ok and stable_result is not None:
                return finish(await retry_api_for_incomplete(stable_result))
            if api_fallback is not None:
                return finish(api_fallback)
            failed = _shop_search_result(
                query,
                shop,
                url=url,
                ok=False,
                error=stable_error,
                criterion=criterion,
                criterion_label=criterion_label,
                search_query=search_query,
                constraints=constraints,
            )
            failed["error_code"] = stable_error_code
            return finish(failed)

        primary_timeout = self.config.shopping_budget_sec
        if can_retry_headful:
            primary_timeout = min(primary_timeout, max(5.0, primary_timeout * 0.4))
        stage_started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._shop_search_impl(
                    query,
                    shop or "",
                    url,
                    max_items,
                    want_cities,
                    criterion,
                    criterion_label,
                    search_query,
                    constraints,
                ),
                timeout=primary_timeout,
            )
            primary_ok = bool(result.get("ok") and result.get("items"))
            primary_error = str(result.get("error") or "no products parsed")
            stages.append(
                {
                    "name": "headless_chromium",
                    "ok": primary_ok,
                    "elapsed_ms": max(0, round((time.monotonic() - stage_started) * 1000)),
                    "error_code": None if primary_ok else "empty_catalog",
                    "error": None if primary_ok else primary_error,
                }
            )
            if primary_ok:
                return finish(await retry_api_for_incomplete(result))
        except TimeoutError:
            primary_error = "shop_search exceeded budget"
            stages.append(
                {
                    "name": "headless_chromium",
                    "ok": False,
                    "elapsed_ms": max(0, round((time.monotonic() - stage_started) * 1000)),
                    "error_code": "headless_timeout",
                    "error": primary_error,
                }
            )
        except AntiBotError as exc:
            primary_error = f"anti-bot: {exc}"
            stages.append(
                {
                    "name": "headless_chromium",
                    "ok": False,
                    "elapsed_ms": max(0, round((time.monotonic() - stage_started) * 1000)),
                    "error_code": "anti_bot",
                    "error": primary_error,
                }
            )
        except WebSurferError as exc:
            primary_error = str(exc)
            stages.append(
                {
                    "name": "headless_chromium",
                    "ok": False,
                    "elapsed_ms": max(0, round((time.monotonic() - stage_started) * 1000)),
                    "error_code": "headless_failure",
                    "error": primary_error,
                }
            )

        # DNS/Qrator currently serves a 401/403 shell to Playwright headless but
        # lets the installed stable Chrome complete its JS proof-of-work.  Retry
        # only on an interactive Windows host; production Linux remains strictly
        # headless and returns the original structured failure.
        if can_retry_headful:
            remaining = self.config.shopping_budget_sec - (time.monotonic() - started)
            if remaining > 3:
                stage_started = time.monotonic()
                try:
                    fallback = await asyncio.wait_for(
                        self._shop_search_headful_chrome(
                            query,
                            shop or "",
                            url,
                            max_items,
                            want_cities,
                            criterion,
                            criterion_label,
                            search_query,
                            constraints,
                        ),
                        timeout=remaining,
                    )
                    fallback_ok = bool(fallback.get("ok") and fallback.get("items"))
                    fallback_error = str(fallback.get("error") or "no products parsed")
                    stages.append(
                        {
                            "name": "stable_chrome",
                            "ok": fallback_ok,
                            "elapsed_ms": max(
                                0, round((time.monotonic() - stage_started) * 1000)
                            ),
                            "error_code": None if fallback_ok else "empty_catalog",
                            "error": None if fallback_ok else fallback_error,
                        }
                    )
                    if fallback_ok:
                        return finish(await retry_api_for_incomplete(fallback))
                    primary_error = f"{primary_error}; stable Chrome: {fallback_error}"
                except TimeoutError:
                    primary_error = f"{primary_error}; stable Chrome retry timed out"
                    stages.append(
                        {
                            "name": "stable_chrome",
                            "ok": False,
                            "elapsed_ms": max(
                                0, round((time.monotonic() - stage_started) * 1000)
                            ),
                            "error_code": "stable_timeout",
                            "error": "stable Chrome retry timed out",
                        }
                    )
                except BrowserLaunchError as exc:
                    primary_error = f"{primary_error}; stable Chrome unavailable: {exc}"
                    stages.append(
                        {
                            "name": "stable_chrome",
                            "ok": False,
                            "elapsed_ms": max(
                                0, round((time.monotonic() - stage_started) * 1000)
                            ),
                            "error_code": "stable_unavailable",
                            "error": str(exc),
                        }
                    )
                except NavigationError as exc:
                    primary_error = f"{primary_error}; stable Chrome navigation failed: {exc}"
                    stages.append(
                        {
                            "name": "stable_chrome",
                            "ok": False,
                            "elapsed_ms": max(
                                0, round((time.monotonic() - stage_started) * 1000)
                            ),
                            "error_code": "stable_navigation",
                            "error": str(exc),
                        }
                    )
                except (PlaywrightError, OSError, WebSurferError) as exc:
                    primary_error = f"{primary_error}; stable Chrome failed: {exc}"
                    stages.append(
                        {
                            "name": "stable_chrome",
                            "ok": False,
                            "elapsed_ms": max(
                                0, round((time.monotonic() - stage_started) * 1000)
                            ),
                            "error_code": "stable_failure",
                            "error": str(exc),
                        }
                    )

        if api_fallback is not None:
            return finish(api_fallback)

        return finish(_shop_search_result(
            query,
            shop,
            url=url,
            ok=False,
            error=primary_error,
            criterion=criterion,
            criterion_label=criterion_label,
            search_query=search_query,
            constraints=constraints,
        ))

    async def _wildberries_api_shop_search(
        self,
        *,
        query: str,
        search_query: str,
        max_items: int,
        criterion: str,
        criterion_label: str,
        constraints: dict[str, float],
    ) -> dict[str, Any]:
        """Use Wildberries' own public catalog response before browser rendering."""

        if self._playwright is None:
            raise WebSurferError("Playwright is not started")
        sort = (
            "priceup"
            if criterion == "price_asc"
            else "pricedown"
            if criterion == "price_desc"
            else "popular"
        )
        params = {
            "appType": "1",
            "curr": "rub",
            "dest": "-1257786",
            "lang": "ru",
            "locale": "ru",
            "query": search_query,
            "resultset": "catalog",
            "sort": sort,
            "spp": "30",
            "suppressSpellcheck": "false",
        }
        request = await self._playwright.request.new_context(
            extra_http_headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.wildberries.ru/",
                "User-Agent": self._next_user_agent(),
            }
        )
        found_items: list[dict[str, Any]] = []
        seen_product_ids: set[str] = set()
        try:
            for variant in _catalog_search_variants(query, criterion):
                params["query"] = variant
                for version in ("v14", "v9"):
                    url = (
                        "https://search.wb.ru/exactmatch/ru/common/"
                        f"{version}/search?{urlencode(params)}"
                    )
                    response = await request.get(url, timeout=8_000)
                    if response.status != 200:
                        continue
                    parsed = await response.json()
                    if not isinstance(parsed, dict):
                        continue
                    variant_items = _wildberries_api_items(parsed)
                    for item in variant_items:
                        product_id = str(item.get("product_id") or "")
                        if product_id in seen_product_ids:
                            continue
                        seen_product_ids.add(product_id)
                        found_items.append(item)
                    if variant_items:
                        break
        finally:
            with suppress(Exception):
                await request.dispose()
        items = _filter_catalog_items_for_query(
            found_items,
            query,
        )
        items = _filter_catalog_constraints(items, constraints)
        for item in items:
            _attach_catalog_metrics(item)
        metric_key = _select_catalog_metric_key(items, criterion)
        ranked = _rank_catalog_items(
            items,
            criterion=criterion,
            metric_key=metric_key,
            target_price=constraints.get("target_price"),
        )[:max_items]
        priced = [
            item
            for item in ranked
            if item.get("price_value") is not None and item.get("in_stock") is not False
        ]
        cheapest = min(priced, key=lambda item: float(item["price_value"])) if priced else None
        best = _best_catalog_item(ranked, criterion=criterion, metric_key=metric_key)
        return _shop_search_result(
            query,
            "wildberries",
            ok=bool(ranked),
            url=_shop_search_url("wildberries", search_query, criterion=criterion),
            city="Москва",
            items=ranked,
            cheapest=cheapest,
            best=best,
            error=None if ranked else "Wildberries API returned no matching products",
            browser_mode="wildberries_catalog_api",
            price_sort_confirmed=criterion in {"price_asc", "price_desc"},
            criterion=criterion,
            criterion_label=criterion_label,
            metric_key=metric_key,
            search_query=search_query,
            constraints=constraints,
        )

    async def _shop_search_impl(
        self,
        query: str,
        shop: str,
        url: str,
        max_items: int,
        cities: list[str],
        criterion: str,
        criterion_label: str,
        search_query: str,
        constraints: dict[str, float],
    ) -> dict[str, Any]:
        context = await self._new_context(
            storage_state_path=self._shop_storage_state_path(shop)
        )
        try:
            page = await self._new_page(context)
            return await self._shop_search_page(
                page,
                query=query,
                shop=shop,
                url=url,
                max_items=max_items,
                cities=cities,
                wait_for_challenge=False,
                browser_mode="headless_chromium",
                criterion=criterion,
                criterion_label=criterion_label,
                search_query=search_query,
                constraints=constraints,
            )
        finally:
            await self._persist_shop_storage_state(context, shop)
            with suppress(Exception):
                await asyncio.wait_for(context.close(), timeout=_CLEANUP_TIMEOUT_SEC)

    async def _shop_search_headful_chrome(
        self,
        query: str,
        shop: str,
        url: str,
        max_items: int,
        cities: list[str],
        criterion: str,
        criterion_label: str,
        search_query: str,
        constraints: dict[str, float],
    ) -> dict[str, Any]:
        """Retry a blocked catalog in the installed stable Chrome.

        This deliberately skips stealth scripts and UA spoofing: mixing an old
        spoofed UA with a current Chrome binary is itself a detectable fingerprint.
        """

        if self._playwright is None:
            raise BrowserLaunchError("Playwright is not started")
        channel = str(self.config.headful_browser_channel or "chrome").strip() or "chrome"
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--window-position=-32000,-32000",
            "--window-size=1366,900",
        ]
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            profile_path = self._shop_persistent_profile_path(shop)
            if profile_path is not None and _normalize_shop(shop) == "dns":
                profile_path.mkdir(parents=True, exist_ok=True)
                persistent_kwargs: dict[str, Any] = {
                    "channel": channel,
                    "headless": False,
                    "locale": self.config.locale,
                    "timezone_id": self.config.timezone_id,
                    "viewport": {
                        "width": self.config.viewport_width,
                        "height": self.config.viewport_height,
                    },
                    "extra_http_headers": {
                        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                        **self.config.extra_headers,
                    },
                    "ignore_https_errors": True,
                    "args": launch_args,
                }
                proxy = self._next_proxy()
                if proxy is not None:
                    persistent_kwargs["proxy"] = proxy
                context = await self._playwright.chromium.launch_persistent_context(
                    str(profile_path),
                    **persistent_kwargs,
                )
                context.set_default_timeout(self.config.default_timeout_ms)
                context.set_default_navigation_timeout(self.config.nav_timeout_ms)
            else:
                browser = await self._playwright.chromium.launch(
                    channel=channel,
                    headless=False,
                    args=launch_args,
                )
                context = await self._new_context(
                    browser=browser,
                    use_config_user_agent=False,
                    storage_state_path=self._shop_storage_state_path(shop),
                )
            if browser is None and context.pages:
                page = context.pages[0]
            else:
                page = await self._new_page(context, use_stealth=False)
            return await self._shop_search_page(
                page,
                query=query,
                shop=shop,
                url=url,
                max_items=max_items,
                cities=cities,
                wait_for_challenge=True,
                browser_mode="headful_stable_chrome",
                criterion=criterion,
                criterion_label=criterion_label,
                search_query=search_query,
                constraints=constraints,
            )
        finally:
            if context is not None:
                await self._persist_shop_storage_state(context, shop)
                context_browser = getattr(context, "browser", None)
                try:
                    await asyncio.wait_for(
                        context.close(),
                        timeout=(
                            _PERSISTENT_CONTEXT_CLEANUP_TIMEOUT_SEC
                            if browser is None
                            else _CLEANUP_TIMEOUT_SEC
                        ),
                    )
                except Exception:
                    if browser is None and context_browser is not None:
                        with suppress(Exception):
                            await asyncio.wait_for(
                                context_browser.close(),
                                timeout=_CLEANUP_TIMEOUT_SEC,
                            )
            if browser is not None:
                with suppress(Exception):
                    await asyncio.wait_for(browser.close(), timeout=_CLEANUP_TIMEOUT_SEC)

    async def _shop_search_page(
        self,
        page: Page,
        *,
        query: str,
        shop: str,
        url: str,
        max_items: int,
        cities: list[str],
        wait_for_challenge: bool,
        browser_mode: str,
        criterion: str,
        criterion_label: str,
        search_query: str,
        constraints: dict[str, float],
    ) -> dict[str, Any]:
        await _install_shop_navigation_guard(page, shop=shop, initial_url=url)
        try:
            response = await self._goto(page, url, wait_until="domcontentloaded")
        except NavigationError as exc:
            if (
                not wait_for_challenge
                or "timed out" not in str(exc).casefold()
                or not await _shop_page_has_product_dom(page)
            ):
                raise
            # Qrator can replace the document with a usable catalog while the
            # original navigation promise still times out. Continue from the
            # independently inspected DOM instead of misclassifying Chrome as
            # unavailable.
            response = None
        catalog_ready = False
        if wait_for_challenge:
            # Qrator initially answers 401, runs a browser proof-of-work, then
            # replaces the page with the catalog without a normal navigation.
            catalog_selector = (
                ".catalog-product:has-text('₽')"
                if _normalize_shop(shop) == "dns"
                else (
                    "a[href*='/product/'], a[href*='/detail.aspx'], "
                    ".catalog-product, article.product-card"
                )
            )
            with suppress(PlaywrightError, PlaywrightTimeoutError):
                await page.wait_for_selector(
                    catalog_selector,
                    timeout=10_000,
                )
            if _normalize_shop(shop) == "dns":
                with suppress(PlaywrightError):
                    catalog_ready = (
                        await page.locator(".catalog-product:has-text('₽')").count()
                        > 0
                    )
            else:
                catalog_ready = await _shop_page_has_product_dom(page)
        sorted_url = _shop_price_sorted_url(shop, page.url or url, criterion=criterion)
        challenge_catalog_ready = (
            wait_for_challenge
            and catalog_ready
            and not cities
            and (not sorted_url or sorted_url == (page.url or ""))
        )
        if challenge_catalog_ready:
            city = ""
            with suppress(PlaywrightError):
                city = _city_label_from_cookies(await page.context.cookies())
        else:
            await self._human_mouse_move(page)
            city = await self._try_set_city(page, cities)
            if not city:
                with suppress(PlaywrightError):
                    city = _city_label_from_cookies(await page.context.cookies())
            if sorted_url and sorted_url != (page.url or ""):
                with suppress(PlaywrightError, PlaywrightTimeoutError, NavigationError):
                    response = await self._goto(page, sorted_url, wait_until="domcontentloaded")
                    await page.wait_for_selector(
                        "a[href*='/product/'], a[href*='/detail.aspx'], "
                        ".catalog-product, article.product-card",
                        timeout=10_000,
                    )
            with suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=9_000)
            for _ in range(3):
                with suppress(PlaywrightError):
                    await page.mouse.wheel(0, 2200)
                await self._human_pause(0.5)
        html = ""
        with suppress(PlaywrightError):
            html = await page.content()
        items = _filter_catalog_items_for_query(
            _extract_catalog_items(html, base_url=page.url or url),
            query,
        )
        price_constraints = {
            key: value
            for key, value in constraints.items()
            if key in {"min_price", "max_price"}
        }
        items = _filter_catalog_constraints(items, price_constraints)
        if (
            criterion not in {"price_asc", "price_desc", "price_nearest"}
            or "min_rating" in constraints
        ):
            if _normalize_shop(shop) == "wildberries":
                await _enrich_wildberries_catalog_items(page.context, items[:max_items])
            else:
                await self._enrich_generic_catalog_items(
                    page.context,
                    items[:8],
                    shop=shop,
                )
        items = _filter_catalog_constraints(items, constraints)
        for item in items:
            _attach_catalog_metrics(item)
        metric_key = _select_catalog_metric_key(items, criterion)
        ranked = _rank_catalog_items(
            items,
            criterion=criterion,
            metric_key=metric_key,
            target_price=constraints.get("target_price"),
        )[:max_items]
        priced = [
            item
            for item in ranked
            if item.get("price_value") is not None and item.get("in_stock") is not False
        ]
        cheapest = min(priced, key=lambda item: float(item["price_value"])) if priced else None
        best = _best_catalog_item(ranked, criterion=criterion, metric_key=metric_key)
        error = None
        if not ranked:
            status = getattr(response, "status", None)
            title = ""
            with suppress(PlaywrightError):
                title = _collapse(await page.title())
            detail = f"HTTP {status}" if isinstance(status, int) and status >= 400 else ""
            if title:
                detail = f"{detail} {title}".strip()
            reason = (
                "no matching products parsed"
            )
            error = f"{detail}: {reason}".lstrip(": ")
        return _shop_search_result(
            query,
            shop,
            url=page.url or url,
            city=city,
            ok=bool(ranked),
            items=ranked,
            cheapest=cheapest,
            best=best,
            error=error,
            browser_mode=browser_mode,
            price_sort_confirmed=_shop_price_sort_confirmed(
                shop,
                page.url or url,
                criterion=criterion,
            ),
            criterion=criterion,
            criterion_label=criterion_label,
            metric_key=metric_key,
            search_query=search_query,
            constraints=constraints,
        )

    async def _enrich_generic_catalog_items(
        self,
        context: BrowserContext,
        items: Sequence[dict[str, Any]],
        *,
        shop: str,
    ) -> None:
        """Read a bounded set of product cards when catalog tiles omit specs."""

        semaphore = asyncio.Semaphore(3)

        async def enrich(item: dict[str, Any]) -> None:
            product_url = str(item.get("url") or "")
            if not product_url.startswith(("http://", "https://")):
                return
            page: Page | None = None
            try:
                async with semaphore:
                    page = await self._new_page(context)
                    await _install_shop_navigation_guard(
                        page,
                        shop=shop,
                        initial_url=product_url,
                    )
                    await self._goto(page, product_url, wait_until="domcontentloaded")
                    with suppress(PlaywrightError):
                        await page.wait_for_load_state("networkidle", timeout=4_000)
                    specs = await self._extract_specs(page)
                    html = ""
                    with suppress(PlaywrightError):
                        html = await page.content()
                    jsonld = self._extract_jsonld(html) if html else []
                    rating = _extract_rating(jsonld)
                    body = ""
                    with suppress(PlaywrightError):
                        body = _collapse(await page.locator("body").inner_text(timeout=3_000))
                if specs:
                    item["specs"] = specs
                if body:
                    item["description"] = body[:2400]
                if isinstance(rating.get("value"), int | float):
                    item["rating_value"] = float(rating["value"])
                if isinstance(rating.get("count"), int):
                    item["review_count"] = int(rating["count"])
            except (NavigationError, PlaywrightError, PlaywrightTimeoutError):
                return
            finally:
                if page is not None:
                    with suppress(Exception):
                        await page.close()

        await asyncio.gather(*(enrich(item) for item in items), return_exceptions=True)

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


# Mojeek is a keyless, JS-free, bot-tolerant HTML search engine used as an
# independent fallback so a DuckDuckGo rate-limit no longer blanks out search.
# Result shape:  <a class="title" ... href="URL">TITLE</a> ... <p class="s">SNIPPET</p>
_MOJEEK_TITLE_RE = re.compile(
    r'<a\s+class="title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_MOJEEK_SNIPPET_RE = re.compile(
    r'<p\s+class="s">(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)


def _parse_mojeek_html(html: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    snippets = _MOJEEK_SNIPPET_RE.findall(html)
    for index, match in enumerate(_MOJEEK_TITLE_RE.finditer(html)):
        url = unescape(match.group(1))
        title = _collapse(unescape(_strip_tags(match.group(2))))
        if not url.startswith("http") or not title:
            continue
        snippet = ""
        if index < len(snippets):
            snippet = _collapse(unescape(_strip_tags(snippets[index])))
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
    snippets: Sequence[dict[str, str]] = (),
) -> str:
    parts: list[str] = [f"# Исследование: {query}", ""]
    if answer:
        parts.append(f"**Кратко:** {answer}")
        parts.append("")
    if not sections:
        # Full page bodies were unreadable (anti-bot / JS walls). Fall back to the
        # search snippets — they usually carry the answer — instead of nothing.
        usable = [item for item in snippets if _collapse(str(item.get("snippet") or ""))]
        if usable:
            parts.append(
                "Полные страницы источников недоступны для автоматического чтения; "
                "ниже — выдержки из результатов поиска:"
            )
            parts.append("")
            for item in usable[:6]:
                title = _collapse(str(item.get("title") or item.get("url") or ""))
                url = str(item.get("url") or "")
                parts.append(f"## {title}")
                if url:
                    parts.append(url)
                parts.append(_collapse(str(item.get("snippet") or "")))
                parts.append("")
            return "\n".join(parts).strip()
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


def _normalize_shop(shop: str | None) -> str:
    source = get_shop_source(shop)
    return source.key if source else str(shop or "").strip().casefold().replace("ё", "е")


async def _install_shop_navigation_guard(
    page: Page,
    *,
    shop: str | None,
    initial_url: str,
) -> None:
    """Abort main-frame redirects outside the selected registered shop."""

    source = get_shop_source(shop) or get_shop_source_by_host(urlparse(initial_url).hostname)
    if source is None:
        raise NavigationError("shop URL is not associated with a registered source")

    async def guard(route: Any) -> None:
        request = route.request
        hostname = (urlparse(request.url).hostname or "").casefold()
        same_shop = hostname == source.domain or hostname.endswith(f".{source.domain}")
        is_main_navigation = False
        with suppress(PlaywrightError):
            is_main_navigation = (
                request.is_navigation_request() and request.frame == page.main_frame
            )
        if is_main_navigation and not same_shop:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    await page.route("**/*", guard)


async def _shop_page_has_product_dom(page: Page) -> bool:
    selectors = (
        "a[href*='/product/'], a[href*='/detail.aspx'], "
        ".catalog-product, article.product-card"
    )
    with suppress(Exception):
        if await page.locator(selectors).count() > 0:
            return True
    with suppress(Exception):
        html = (await page.content()).casefold()
        return any(
            marker in html
            for marker in ("/product/", "/detail.aspx", "catalog-product", "product-card")
        )
    return False


def _shop_search_url(
    shop: str | None,
    query: str,
    *,
    criterion: str = "price_asc",
) -> str:
    url = registry_shop_search_url(shop, query)
    if not url:
        return ""
    return _shop_price_sorted_url(shop, url, criterion=criterion)


def _shop_price_sorted_url(
    shop: str | None,
    url: str,
    *,
    criterion: str = "price_asc",
) -> str:
    """Reapply a shop's price sort after anti-bot and city redirects."""

    if criterion != "price_asc":
        return url
    normalized_shop = _normalize_shop(shop)
    parsed = urlparse(url)
    if normalized_shop == "dns" or (parsed.hostname or "").endswith("dns-shop.ru"):
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["order"] = ["price"]
        query["stock"] = ["all"]
        return parsed._replace(query=urlencode(query, doseq=True)).geturl()
    if normalized_shop == "ozon" or (parsed.hostname or "").endswith("ozon.ru"):
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["sorting"] = ["price"]
        return parsed._replace(query=urlencode(query, doseq=True)).geturl()
    return url


def _shop_price_sort_confirmed(
    shop: str | None,
    url: str,
    *,
    criterion: str = "price_asc",
) -> bool:
    if criterion != "price_asc":
        return False
    normalized_shop = _normalize_shop(shop)
    query = parse_qs(urlparse(url).query)
    if normalized_shop == "dns":
        return query.get("order", [""])[0].casefold() == "price"
    if normalized_shop == "ozon":
        return query.get("sorting", [""])[0].casefold() == "price"
    return False


_CATALOG_PRICE_RE = re.compile(
    r"(?P<num>\d[\d\s ]{2,})\s*(?:₽|руб(?:\.|лей|ля)?|р\.)"
    r"|(?:₽|руб)\s*(?P<num2>\d[\d\s ]{2,})",
    flags=re.IGNORECASE,
)
_PRODUCT_HREF_RE = re.compile(
    r"/(?:product(?:s)?|catalog|goods|p|item|detail|dp|card|context/detail)(?:/|--)",
    re.IGNORECASE,
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

    wildberries_items = _catalog_from_wildberries(soup, base_url)
    if wildberries_items:
        return wildberries_items
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
        host = (urlparse(base_url).hostname or "").casefold()
        if host.endswith("wildberries.ru") and not re.search(
            r"/catalog/\d+/detail\.aspx(?:$|[?#])",
            href,
            flags=re.IGNORECASE,
        ):
            continue
        if (urlparse(base_url).hostname or "").endswith("dns-shop.ru") and not re.search(
            r"/product/", href, flags=re.IGNORECASE
        ):
            continue
        url = _canonical_catalog_product_url(urljoin(base_url, href) if base_url else href)
        key = url.split("?", 1)[0]
        if key in seen:
            continue
        price_text = _nearest_price(anchor)
        if not price_text:
            continue
        in_stock = _catalog_item_stock(anchor, href)
        seen.add(key)
        results.append(
            {
                "title": title[:200],
                "url": url,
                "price_text": price_text,
                "price_value": _parse_amount(price_text),
                "in_stock": in_stock,
            }
        )
    return results


def _catalog_from_wildberries(soup: Any, base_url: str) -> list[dict[str, Any]]:
    host = (urlparse(base_url).hostname or "").casefold()
    if not host.endswith("wildberries.ru"):
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select("article.product-card[data-nm-id]"):
        if not isinstance(card, Tag):
            continue
        product_id = str(card.get("data-nm-id") or "").strip()
        anchor = card.select_one("a[href*='/detail.aspx']")
        if not product_id.isdigit() or not isinstance(anchor, Tag):
            continue
        href = str(anchor.get("href") or "")
        if not re.search(r"/catalog/\d+/detail\.aspx(?:$|[?#])", href, re.IGNORECASE):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        title = _collapse(str(anchor.get("aria-label") or ""))
        if not title:
            title_node = card.select_one(".product-card__brand-wrap")
            title = _collapse(title_node.get_text(" ", strip=True) if title_node else "")
        if len(title) < 3:
            continue
        price_node = card.select_one("ins")
        price_text = _collapse(price_node.get_text(" ", strip=True) if price_node else "")
        if not price_text:
            price_text = _nearest_price(anchor)
        if price_text and "₽" not in price_text:
            price_text = f"{price_text} ₽"
        lines = [_collapse(value) for value in card.stripped_strings if _collapse(value)]
        rating_value: float | None = None
        review_count: int | None = None
        for index, line in enumerate(lines[:-1]):
            if not re.fullmatch(r"[1-5](?:[,.]\d)?", line):
                continue
            count_match = re.search(r"(\d[\d\s]*)\s+(?:оцен|отзыв)", lines[index + 1])
            if not count_match:
                continue
            rating_value = float(line.replace(",", "."))
            review_count = int(re.sub(r"\D", "", count_match.group(1)))
            break
        image = card.select_one("img")
        image_url = ""
        if isinstance(image, Tag):
            image_url = str(
                image.get("src")
                or image.get("data-src-pb")
                or image.get("data-original")
                or ""
            )
        details_url = _wildberries_details_url(image_url, product_id)
        seen.add(url)
        items.append(
            {
                "title": title[:300],
                "url": url,
                "product_id": product_id,
                "price_text": price_text,
                "price_value": _parse_amount(price_text),
                "in_stock": None,
                "rating_value": rating_value,
                "review_count": review_count,
                "source_text": " ".join(lines)[:1500],
                "details_url": details_url,
            }
        )
    return items


def _wildberries_details_url(image_url: str, product_id: str) -> str:
    parsed = urlparse(image_url)
    if not (parsed.hostname or "").endswith(".wbbasket.ru"):
        return ""
    marker = f"/{product_id}/images/"
    if marker not in parsed.path:
        return ""
    prefix = parsed.path.split(marker, 1)[0]
    return parsed._replace(
        path=f"{prefix}/{product_id}/info/ru/card.json",
        params="",
        query="",
        fragment="",
    ).geturl()


_CATALOG_QUERY_STOPWORDS = {
    "buy",
    "cheap",
    "cheapest",
    "find",
    "in",
    "price",
    "shop",
    "store",
    "в",
    "где",
    "дешево",
    "дешевую",
    "дешёвую",
    "купить",
    "магазин",
    "магазине",
    "на",
    "найди",
    "самую",
    "цена",
}
_CATALOG_GENERIC_TOKENS = {
    "gb",
    "geforce",
    "gpu",
    "hdd",
    "radeon",
    "rtx",
    "series",
    "ssd",
    "tb",
    "гб",
    "тб",
}
_CATALOG_CATEGORY_PREFIXES = (
    "видеокарт",
    "клавиатур",
    "монитор",
    "мыш",
    "наушник",
    "накопител",
    "ноутбук",
    "планшет",
    "процессор",
    "смартфон",
    "телевизор",
    "телефон",
)


def _catalog_match_tokens(value: str) -> set[str]:
    normalized = str(value or "").casefold().replace("ё", "е")
    tokens: set[str] = set()
    for token in re.findall(r"[a-zа-я0-9]+", normalized):
        if any(char.isalpha() for char in token) and any(char.isdigit() for char in token):
            tokens.update(re.findall(r"[a-zа-я]+|\d+", token))
        else:
            tokens.add(token)
    aliases = {"tb": "тб", "gb": "гб"}
    for source, target in aliases.items():
        if source in tokens:
            tokens.add(target)
        if target in tokens:
            tokens.add(source)
    return tokens


def _catalog_token_matches(token: str, title_tokens: set[str]) -> bool:
    if token in title_tokens:
        return True
    if len(token) < 4 or not re.fullmatch(r"[а-я]+", token):
        return False
    stem_length = 4 if len(token) <= 5 else 5
    stem = token[:stem_length]
    return any(
        len(candidate) >= stem_length
        and re.fullmatch(r"[а-я]+", candidate) is not None
        and candidate.startswith(stem)
        for candidate in title_tokens
    )


def _city_label_from_cookies(cookies: Sequence[dict[str, Any]]) -> str:
    aliases = {
        "donetsk": "Донецк",
        "moscow": "Москва",
        "moskva": "Москва",
        "saint-petersburg": "Санкт-Петербург",
        "sankt-peterburg": "Санкт-Петербург",
        "spb": "Санкт-Петербург",
    }
    for cookie in cookies:
        name = str(cookie.get("name") or "").casefold()
        if name not in {"city", "city_path", "location_city"}:
            continue
        value = unquote(str(cookie.get("value") or "")).strip().casefold()
        if not value:
            continue
        return aliases.get(value, value.replace("-", " ").title())
    return ""


def _filter_catalog_items_for_query(
    items: Sequence[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """Drop fuzzy-search neighbours before price ranking.

    Shop search pages commonly mix nearby models (``5060``/``5070`` for a
    ``5090`` query).  Ranking the raw grid therefore produces a confidently
    wrong cheapest result.  Numeric/model tokens are strict; descriptive words
    use a small overlap threshold so ordinary natural-language product queries
    still work.
    """

    raw_query_tokens = _catalog_match_tokens(query)
    stopwords = {token.replace("ё", "е") for token in _CATALOG_QUERY_STOPWORDS}
    query_tokens = {
        token
        for token in raw_query_tokens
        if token not in stopwords
        and not any(token.startswith(prefix) for prefix in _CATALOG_CATEGORY_PREFIXES)
        and (len(token) >= 2 or token.isdigit())
    }
    if not query_tokens:
        return list(items)
    numeric = {token for token in query_tokens if token.isdigit()}
    model_tokens = {
        token
        for token in query_tokens
        if any(char.isdigit() for char in token) and any(char.isalpha() for char in token)
    }
    lexical = query_tokens - numeric - model_tokens
    differentiators = lexical - _CATALOG_GENERIC_TOKENS
    minimum_lexical = 0 if not lexical else max(1, (len(lexical) + 1) // 2)
    matched: list[dict[str, Any]] = []
    for item in items:
        title_tokens = _catalog_match_tokens(str(item.get("title") or ""))
        if numeric and not numeric.issubset(title_tokens):
            continue
        if model_tokens and not model_tokens.issubset(title_tokens):
            continue
        if differentiators and not all(
            _catalog_token_matches(token, title_tokens) for token in differentiators
        ):
            continue
        lexical_overlap = sum(_catalog_token_matches(token, title_tokens) for token in lexical)
        if lexical and lexical_overlap < minimum_lexical:
            continue
        matched.append(item)
    return matched


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
                availability = _jsonld_offer_field([node], "availability").casefold()
                in_stock = None
                if availability:
                    in_stock = "instock" in availability and "outofstock" not in availability
                items.append(
                    {
                        "title": name[:200],
                        "url": _canonical_catalog_product_url(
                            urljoin(base_url, url) if (base_url and url) else url
                        ),
                        "price_text": f"{price} ₽" if price else "",
                        "price_value": _parse_amount(price),
                        "in_stock": in_stock,
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


def _catalog_item_stock(anchor: Tag, href: str) -> bool | None:
    """Best-effort purchasability from the product card around an anchor."""

    card: Tag | None = None
    node: Any = anchor
    for _ in range(8):
        node = node.parent
        if node is None or not isinstance(node, Tag):
            break
        classes = {str(item) for item in (node.get("class") or [])}
        if "catalog-product" in classes or "product-card" in classes:
            card = node
            break
    text = _collapse((card or anchor.parent or anchor).get_text(" ", strip=True)).casefold()
    if "/product/analog/" in href.casefold() or any(
        marker in text
        for marker in (
            "аналоги",
            "нет в наличии",
            "нет в продаже",
            "not in stock",
            "currently unavailable",
            "уведомить о поступлении",
            "out of stock",
        )
    ):
        return False
    if any(marker in text for marker in ("купить", "в корзину", "add to cart", "in stock")):
        return True
    return None


def _canonical_catalog_product_url(url: str) -> str:
    """Turn DNS's availability-dependent ``/product/analog/`` link into the card URL."""

    value = str(url)
    host = (urlparse(value).hostname or "").casefold()
    if host == "dns-shop.ru" or host.endswith(".dns-shop.ru"):
        return re.sub(r"/product/analog/", "/product/", value, count=1, flags=re.IGNORECASE)
    return value


def _wildberries_api_products(payload: dict[str, Any]) -> list[dict[str, Any]]:
    products = payload.get("products")
    if not isinstance(products, list):
        data = payload.get("data")
        products = data.get("products") if isinstance(data, dict) else []
    return [item for item in products if isinstance(item, dict)]


def _wildberries_api_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for product in _wildberries_api_products(payload):
        product_id = str(product.get("id") or "").strip()
        if not product_id.isdigit():
            continue
        name = _collapse(str(product.get("name") or ""))
        brand = _collapse(str(product.get("brand") or ""))
        title = _collapse(" / ".join(value for value in (brand, name) if value))
        if not title:
            continue
        prices: list[float] = []
        quantities: list[int] = []
        for size in product.get("sizes") or []:
            if not isinstance(size, dict):
                continue
            price = size.get("price")
            if isinstance(price, dict):
                # Compare the ordinary card price. ``wallet`` is conditional on
                # using WB Wallet and would make the result look universally cheaper.
                for key in ("product", "basic", "wallet"):
                    raw = price.get(key)
                    if isinstance(raw, int | float) and raw > 0:
                        prices.append(float(raw) / 100.0)
                        break
            for stock in size.get("stocks") or []:
                if isinstance(stock, dict):
                    with suppress(TypeError, ValueError):
                        quantities.append(int(stock.get("qty") or 0))
        total_quantity = product.get("totalQuantity")
        if isinstance(total_quantity, int | float):
            quantities.append(int(total_quantity))
        price_value = min(prices) if prices else None
        rating = product.get("reviewRating") or product.get("nmReviewRating")
        feedbacks = product.get("feedbacks") or product.get("nmFeedbacks")
        items.append(
            {
                "title": title[:300],
                "url": f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
                "product_id": product_id,
                "price_text": (
                    f"{int(price_value):,} ₽".replace(",", " ")
                    if price_value is not None
                    else ""
                ),
                "price_value": price_value,
                "in_stock": any(value > 0 for value in quantities) if quantities else None,
                "rating_value": float(rating) if isinstance(rating, int | float) else None,
                "review_count": int(feedbacks) if isinstance(feedbacks, int | float) else None,
                "source_text": title,
            }
        )
    return items


async def _enrich_wildberries_catalog_items(
    context: BrowserContext,
    items: Sequence[dict[str, Any]],
) -> None:
    semaphore = asyncio.Semaphore(6)

    async def enrich(item: dict[str, Any]) -> None:
        details_url = str(item.get("details_url") or "")
        if not details_url or not (urlparse(details_url).hostname or "").endswith(
            ".wbbasket.ru"
        ):
            return
        try:
            async with semaphore:
                response = await context.request.get(details_url, timeout=8_000)
            if response.status != 200:
                return
            payload = await response.json()
        except (PlaywrightError, ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        specs: dict[str, str] = {}
        option_groups: list[Any] = [payload.get("options") or []]
        for group in payload.get("grouped_options") or []:
            if isinstance(group, dict):
                option_groups.append(group.get("options") or [])
        for options in option_groups:
            for option in options if isinstance(options, list) else []:
                if not isinstance(option, dict):
                    continue
                name = _collapse(str(option.get("name") or ""))
                value = _collapse(str(option.get("value") or ""))
                if name and value and name not in specs:
                    specs[name[:160]] = value[:300]
        item["specs"] = specs
        item["description"] = _collapse(str(payload.get("description") or ""))[:1200]
        item["category"] = _collapse(str(payload.get("subj_name") or ""))[:160]

    await asyncio.gather(*(enrich(item) for item in items), return_exceptions=True)


_CATALOG_CRITERIA = {
    "price_asc",
    "price_desc",
    "price_nearest",
    "power_desc",
    "speed_desc",
    "capacity_desc",
    "range_desc",
    "runtime_desc",
    "age_asc",
    "age_desc",
    "size_asc",
    "size_desc",
    "date_desc",
    "rating_desc",
    "popularity_desc",
    "weight_asc",
    "weight_desc",
}


def _normalize_catalog_criterion(value: str) -> str:
    criterion = str(value or "price_asc").strip().casefold()
    return criterion if criterion in _CATALOG_CRITERIA else "price_asc"


def _normalize_catalog_constraints(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, float] = {}
    for key in ("max_price", "min_price", "min_rating", "target_price"):
        try:
            number = float(value.get(key))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number) or number < 0:
            continue
        if key == "min_rating" and number > 5:
            continue
        normalized[key] = number
    if (
        "min_price" in normalized
        and "max_price" in normalized
        and normalized["min_price"] > normalized["max_price"]
    ):
        normalized.pop("min_price")
        normalized.pop("max_price")
    return normalized


def _filter_catalog_constraints(
    items: Sequence[dict[str, Any]],
    constraints: dict[str, float] | None,
) -> list[dict[str, Any]]:
    normalized = _normalize_catalog_constraints(constraints)
    if not normalized:
        return list(items)
    filtered: list[dict[str, Any]] = []
    for item in items:
        price = item.get("price_value")
        rating = item.get("rating_value")
        if "max_price" in normalized and (
            not isinstance(price, int | float) or float(price) > normalized["max_price"]
        ):
            continue
        if "min_price" in normalized and (
            not isinstance(price, int | float) or float(price) < normalized["min_price"]
        ):
            continue
        if "min_rating" in normalized and (
            not isinstance(rating, int | float) or float(rating) < normalized["min_rating"]
        ):
            continue
        filtered.append(item)
    return filtered


def _catalog_search_query(query: str, criterion: str) -> str:
    """Keep the primary catalog query neutral; criteria belong in ranking."""

    return _collapse(query)


def _catalog_search_variants(query: str, criterion: str) -> list[str]:
    """Add one recall-oriented variant without replacing the neutral query."""

    hints = {
        "power_desc": "мощный",
        "speed_desc": "быстрый",
        "capacity_desc": "большая емкость",
        "range_desc": "большая дальность",
        "runtime_desc": "долгая автономная работа",
        "rating_desc": "лучший",
        "date_desc": "новинка",
        "age_asc": "новинка",
        "age_desc": "старый выпуск",
    }
    base = _collapse(query)
    hint = hints.get(criterion, "")
    normalized = query.casefold().replace("ё", "е")
    if hint and hint.casefold().replace("ё", "е") not in normalized:
        # Marketplaces weigh leading words heavily. Put the requested criterion
        # first, then merge the neutral catalog query when rate limits allow it.
        return [_collapse(f"{hint} {base}"), base]
    return [base]


def _metric_number(value: str) -> float | None:
    raw = re.sub(r"[\s ]", "", value).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _metric_record(
    *,
    value: float,
    text: str,
    unit: str,
    source: str,
) -> dict[str, Any]:
    return {
        "value": round(float(value), 6),
        "text": _collapse(text),
        "unit": unit,
        "source": source,
    }


def _power_metric(text: str, *, source: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    number = r"\d(?:[\d\s ]*\d)?(?:[.,]\d+)?"

    def factor(raw_unit: str) -> float | None:
        if raw_unit in {"mW", "mw", "мВт", "мвт"}:
            return 0.001
        if raw_unit in {"MW", "МВт"}:
            return 1_000_000.0
        if raw_unit in {"kW", "kw", "кВт", "квт"}:
            return 1000.0
        if raw_unit in {"W", "w", "Вт", "вт"}:
            return 1.0
        return None

    units = r"(?:MW|МВт|mW|мВт|kW|кВт|W|Вт|mw|мвт|kw|квт|w|вт)"
    for match in re.finditer(
        rf"(?<![A-Za-zА-Яа-яЁё0-9])(?P<count>{number})\s*[x×]\s*"
        rf"(?P<each>{number})\s*(?P<unit>{units})(?![A-Za-zА-Яа-яЁё])",
        text,
    ):
        count = _metric_number(match.group("count"))
        each = _metric_number(match.group("each"))
        unit_factor = factor(match.group("unit"))
        if count is None or each is None or unit_factor is None:
            continue
        candidates.append(
            _metric_record(
                value=count * each * unit_factor,
                text=match.group(0),
                unit="W",
                source=source,
            )
        )
    for match in re.finditer(
        rf"(?<![A-Za-zА-Яа-яЁё0-9])(?P<value>{number})\s*"
        rf"(?P<unit>{units})(?![A-Za-zА-Яа-яЁё])",
        text,
    ):
        value = _metric_number(match.group("value"))
        unit_factor = factor(match.group("unit"))
        if value is None or unit_factor is None:
            continue
        candidates.append(
            _metric_record(
                value=value * unit_factor,
                text=match.group(0),
                unit="W",
                source=source,
            )
        )
    return max(candidates, key=lambda item: float(item["value"])) if candidates else None


def _single_metric(
    text: str,
    *,
    pattern: str,
    factors: dict[str, float],
    unit: str,
    source: str,
    flags: int = re.IGNORECASE,
    normalize_unit: bool = True,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(pattern, text, flags=flags):
        value = _metric_number(match.group("value"))
        raw_unit = match.group("unit").casefold() if normalize_unit else match.group("unit")
        if value is None or raw_unit not in factors:
            continue
        candidates.append(
            _metric_record(
                value=value * factors[raw_unit],
                text=match.group(0),
                unit=unit,
                source=source,
            )
        )
    return max(candidates, key=lambda item: float(item["value"])) if candidates else None


def _dimensions_metric(text: str, *, source: str) -> dict[str, Any] | None:
    number = r"\d+(?:[.,]\d+)?"
    match = re.search(
        rf"(?P<a>{number})\s*[x×х]\s*(?P<b>{number})\s*[x×х]\s*"
        rf"(?P<c>{number})\s*(?P<unit>мм|mm|см|cm|м|m)(?![A-Za-zА-Яа-яЁё])",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    values = [_metric_number(match.group(name)) for name in ("a", "b", "c")]
    if any(value is None or value <= 0 for value in values):
        return None
    factor = {
        "мм": 0.1,
        "mm": 0.1,
        "см": 1.0,
        "cm": 1.0,
        "м": 100.0,
        "m": 100.0,
    }.get(match.group("unit").casefold())
    if factor is None:
        return None
    a, b, c = (float(value) * factor for value in values if value is not None)
    return _metric_record(
        value=a * b * c,
        text=match.group(0),
        unit="cm³",
        source=source,
    )


def _attach_catalog_metrics(item: dict[str, Any]) -> None:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    segments: list[tuple[str, str]] = [("title", str(item.get("title") or ""))]
    specs = item.get("specs")
    if isinstance(specs, dict):
        segments = [
            (f"spec:{name}", f"{name}: {value}")
            for name, value in specs.items()
        ] + segments
    for source, text in segments:
        source_key = source.casefold()
        power = _power_metric(text, source=source)
        if power and (source == "title" or any(key in source_key for key in ("мощ", "power"))):
            current = metrics.get("power_w")
            if current is None or float(power["value"]) > float(current["value"]):
                metrics["power_w"] = power
        speed_kmh = _single_metric(
            text,
            pattern=r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>км/ч|km/h)\b",
            factors={"км/ч": 1.0, "km/h": 1.0},
            unit="km/h",
            source=source,
        )
        if speed_kmh:
            metrics["speed_kmh"] = speed_kmh
        data_rate = _single_metric(
            text,
            pattern=(
                r"(?P<value>\d+(?:[.,]\d+)?)\s*"
                r"(?P<unit>GB/s|Gb/s|MB/s|Mb/s|ГБ/с|Гб/с|МБ/с|Мб/с)"
                r"(?![A-Za-zА-Яа-яЁё])"
            ),
            factors={
                "GB/s": 8000.0,
                "Gb/s": 1000.0,
                "MB/s": 8.0,
                "Mb/s": 1.0,
                "ГБ/с": 8000.0,
                "Гб/с": 1000.0,
                "МБ/с": 8.0,
                "Мб/с": 1.0,
            },
            unit="Mb/s",
            source=source,
            flags=0,
            normalize_unit=False,
        )
        if data_rate:
            metrics["data_rate_mbps"] = data_rate
        rpm = _single_metric(
            text,
            pattern=r"(?P<value>\d[\d\s]*)\s*(?P<unit>об/мин|rpm)\b",
            factors={"об/мин": 1.0, "rpm": 1.0},
            unit="rpm",
            source=source,
        )
        if rpm:
            metrics["rpm"] = rpm
        capacity = _single_metric(
            text,
            pattern=(
                r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>тб|tb|гб|gb)"
                r"(?!\s*/\s*[сs])(?![A-Za-zА-Яа-яЁё])"
            ),
            factors={"тб": 1000.0, "tb": 1000.0, "гб": 1.0, "gb": 1.0},
            unit="GB",
            source=source,
        )
        if capacity:
            metrics["capacity_gb"] = capacity
        battery = _single_metric(
            text,
            pattern=r"(?P<value>\d[\d\s]*)\s*(?P<unit>мач|mah)\b",
            factors={"мач": 1.0, "mah": 1.0},
            unit="mAh",
            source=source,
        )
        if battery:
            metrics["battery_mah"] = battery
        if any(key in text.casefold() for key in ("дальн", "радиус", "дистанц", "луч")):
            distance = _single_metric(
                text,
                pattern=r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>км|km|м|m)\b",
                factors={"км": 1000.0, "km": 1000.0, "м": 1.0, "m": 1.0},
                unit="m",
                source=source,
            )
            if distance:
                metrics["range_m"] = distance
        if any(key in source_key for key in ("время", "автоном", "работ")):
            runtime = _single_metric(
                text,
                pattern=r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ч|час|часов|h)\b",
                factors={"ч": 1.0, "час": 1.0, "часов": 1.0, "h": 1.0},
                unit="h",
                source=source,
            )
            if runtime:
                metrics["runtime_h"] = runtime
        if any(key in source_key for key in ("вес", "масса", "weight")):
            mass = _single_metric(
                text,
                pattern=r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>кг|kg|г|g)\b",
                factors={"кг": 1.0, "kg": 1.0, "г": 0.001, "g": 0.001},
                unit="kg",
                source=source,
            )
            if mass:
                metrics["mass_kg"] = mass
        if any(key in source_key for key in ("размер", "габарит", "dimension")):
            dimensions = _dimensions_metric(text, source=source)
            if dimensions:
                metrics["size_cm3"] = dimensions
    rating = item.get("rating_value")
    if isinstance(rating, int | float):
        reviews = item.get("review_count")
        review_count = max(0, int(reviews)) if isinstance(reviews, int | float) else 1
        confidence_weight = 20.0
        adjusted = (
            float(rating) * review_count + 4.0 * confidence_weight
        ) / (review_count + confidence_weight)
        rating_text = f"{float(rating):g}/5"
        if isinstance(reviews, int | float):
            rating_text += f", {int(reviews)} отзывов"
        metrics["rating_score"] = _metric_record(
            value=adjusted,
            text=rating_text,
            unit="/5",
            source="catalog",
        )
    reviews = item.get("review_count")
    if isinstance(reviews, int | float):
        metrics["review_count"] = _metric_record(
            value=float(reviews),
            text=f"{int(reviews)} отзывов",
            unit="reviews",
            source="catalog",
        )
    year_match = re.search(r"\b(20\d{2})\b", str(item.get("title") or ""))
    if year_match:
        metrics["year"] = _metric_record(
            value=float(year_match.group(1)),
            text=year_match.group(1),
            unit="year",
            source="title",
        )
    item["metrics"] = metrics


_CRITERION_METRIC_KEYS = {
    "power_desc": ("power_w",),
    "speed_desc": ("data_rate_mbps", "speed_kmh", "rpm"),
    "capacity_desc": ("capacity_gb", "battery_mah"),
    "range_desc": ("range_m",),
    "runtime_desc": ("runtime_h",),
    "size_asc": ("size_cm3",),
    "size_desc": ("size_cm3",),
    "weight_asc": ("mass_kg",),
    "weight_desc": ("mass_kg",),
    "date_desc": ("year",),
    "age_asc": ("year",),
    "age_desc": ("year",),
    "rating_desc": ("rating_score",),
    "popularity_desc": ("review_count",),
}


def _select_catalog_metric_key(items: Sequence[dict[str, Any]], criterion: str) -> str:
    if criterion in {"price_asc", "price_desc", "price_nearest"}:
        return "price_value"
    candidates = _CRITERION_METRIC_KEYS.get(criterion, ())
    if not candidates:
        return ""
    coverage = {
        key: sum(
            isinstance(item.get("metrics"), dict)
            and isinstance(item["metrics"].get(key), dict)
            for item in items
        )
        for key in candidates
    }
    return max(candidates, key=lambda key: coverage[key]) if any(coverage.values()) else ""


def _catalog_metric(item: dict[str, Any], metric_key: str) -> dict[str, Any] | None:
    if metric_key == "price_value":
        value = item.get("price_value")
        if not isinstance(value, int | float):
            return None
        return _metric_record(
            value=float(value),
            text=str(item.get("price_text") or value),
            unit="RUB",
            source="catalog",
        )
    metrics = item.get("metrics")
    metric = metrics.get(metric_key) if isinstance(metrics, dict) else None
    return metric if isinstance(metric, dict) else None


def _rank_catalog_items(
    items: Sequence[dict[str, Any]],
    *,
    criterion: str = "price_asc",
    metric_key: str = "",
    target_price: float | None = None,
) -> list[dict[str, Any]]:
    descending = criterion not in {
        "price_asc",
        "size_asc",
        "weight_asc",
        "age_desc",
    }

    def _key(item: dict[str, Any]) -> tuple[int, int, float, int]:
        stock = item.get("in_stock")
        out_of_stock = 1 if stock is False else 0
        availability_tie = 0 if stock is True else 1 if stock is None else 2
        effective_metric_key = metric_key
        if not effective_metric_key and criterion in {"price_asc", "price_desc", "price_nearest"}:
            effective_metric_key = "price_value"
        metric = _catalog_metric(item, effective_metric_key)
        if metric is None:
            return (out_of_stock, 1, 0.0, availability_tie)
        value = float(metric["value"])
        if criterion == "price_nearest" and isinstance(target_price, int | float):
            metric_value = abs(value - float(target_price))
        else:
            metric_value = -value if descending else value
        return (out_of_stock, 0, metric_value, availability_tie)

    return sorted(items, key=_key)


def _best_catalog_item(
    items: Sequence[dict[str, Any]],
    *,
    criterion: str,
    metric_key: str,
) -> dict[str, Any] | None:
    for item in items:
        if item.get("in_stock") is False:
            continue
        if _catalog_metric(item, metric_key) is not None:
            return item
    return None


def _public_catalog_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    public = dict(item)
    public.pop("details_url", None)
    public.pop("source_text", None)
    if public.get("description"):
        public["description"] = str(public["description"])[:600]
    specs = public.get("specs")
    if isinstance(specs, dict):
        public["specs"] = dict(list(specs.items())[:20])
    return public


def _catalog_metric_label(metric_key: str) -> str:
    return {
        "price_value": "цена",
        "power_w": "мощность",
        "data_rate_mbps": "скорость передачи данных",
        "speed_kmh": "скорость",
        "rpm": "частота вращения",
        "capacity_gb": "ёмкость",
        "battery_mah": "ёмкость аккумулятора",
        "range_m": "дальность",
        "runtime_h": "время автономной работы",
        "mass_kg": "масса",
        "size_cm3": "габаритный объём",
        "year": "год",
        "rating_score": "рейтинг с учётом числа отзывов",
        "review_count": "популярность по числу отзывов",
    }.get(metric_key, metric_key)


def _shop_search_result(
    query: str,
    shop: str | None,
    *,
    ok: bool,
    url: str = "",
    city: str = "",
    items: list[dict[str, Any]] | None = None,
    cheapest: dict[str, Any] | None = None,
    best: dict[str, Any] | None = None,
    error: str | None = None,
    browser_mode: str = "",
    price_sort_confirmed: bool = False,
    criterion: str = "price_asc",
    criterion_label: str = "",
    metric_key: str = "",
    search_query: str = "",
    constraints: dict[str, float] | None = None,
) -> dict[str, Any]:
    rows = [_public_catalog_item(item) for item in (items or [])]
    public_rows = [item for item in rows if item is not None]
    public_cheapest = _public_catalog_item(cheapest)
    public_best = _public_catalog_item(best)
    compared_count = sum(
        _catalog_metric(
            item,
            metric_key
            or (
                "price_value"
                if criterion in {"price_asc", "price_desc", "price_nearest"}
                else ""
            ),
        )
        is not None
        for item in (items or [])
        if item.get("in_stock") is not False
    )
    is_price = criterion in {"price_asc", "price_desc", "price_nearest"}
    comparison_complete = public_best is not None and (is_price or compared_count >= 2)
    output_best = public_best if comparison_complete else None
    comparison = {
        "criterion": criterion,
        "criterion_label": criterion_label,
        "metric_key": metric_key,
        "metric_label": _catalog_metric_label(metric_key),
        "complete": comparison_complete,
        "compared_count": compared_count,
        "discovered_count": len(public_rows),
        "scope": "seller_declared_catalog_items",
        "best_metric": (
            _catalog_metric(best, metric_key)
            if comparison_complete and isinstance(best, dict) and metric_key
            else None
        ),
    }
    target_price = (constraints or {}).get("target_price")
    if criterion == "price_nearest" and isinstance(target_price, int | float):
        comparison["target_price"] = float(target_price)
        best_price = best.get("price_value") if isinstance(best, dict) else None
        if isinstance(best_price, int | float):
            comparison["distance_to_target"] = abs(float(best_price) - float(target_price))
    return {
        "ok": ok,
        "query": query,
        "shop": _normalize_shop(shop) or (shop or ""),
        "url": url,
        "city": city,
        "search_query": search_query or query,
        "constraints": _normalize_catalog_constraints(constraints),
        "count": len(public_rows),
        "cheapest": public_cheapest,
        "best": output_best,
        "comparison": comparison,
        "items": public_rows,
        "error": error,
        "browser_mode": browser_mode,
        "price_sort_confirmed": price_sort_confirmed,
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
