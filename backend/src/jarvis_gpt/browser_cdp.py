from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import websockets
from websockets.exceptions import WebSocketException

DEFAULT_CHROME_DEBUG_URL = "http://127.0.0.1:9222"
LOCAL_DEBUG_HOSTS = {"127.0.0.1", "localhost", "::1"}
UrlValidator = Callable[[str], str | None]

HUMAN_VERIFICATION_MARKERS = (
    "verify you are human",
    "checking your browser",
    "just a moment",
    "captcha",
    "cf-challenge",
    "cf-turnstile",
    "unusual traffic",
    "access denied",
)


class BrowserCdpError(RuntimeError):
    """Raised when a Chrome DevTools Protocol operation cannot complete safely."""


@dataclass(frozen=True)
class BrowserPageSnapshot:
    title: str
    url: str
    ready_state: str
    text: str
    truncated: bool
    needs_human_verification: bool
    form_count: int = 0
    password_input_count: int = 0
    sensitive_input_count: int = 0


@dataclass(frozen=True)
class BrowserTarget:
    id: str
    url: str
    web_socket_debugger_url: str


@dataclass(frozen=True)
class BrowserActionResult:
    action: str
    url: str
    title: str
    ready_state: str
    ok: bool
    summary: str
    snapshot: BrowserPageSnapshot
    screenshot_png: bytes | None = None
    selector: str = ""
    target: str = ""
    target_info: dict[str, Any] | None = None


async def chrome_debugger_status(debug_url: str = DEFAULT_CHROME_DEBUG_URL) -> dict[str, Any]:
    base_url = normalize_debug_url(debug_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False) as client:
            response = await client.get(f"{base_url}/json/version")
            response.raise_for_status()
            version = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "summary": f"Chrome DevTools endpoint is unavailable: {exc}",
            "debug_url": base_url,
        }
    return {
        "ok": True,
        "summary": "Chrome DevTools endpoint is reachable.",
        "debug_url": base_url,
        "version": version,
    }


async def read_chrome_page(
    *,
    url: str,
    max_chars: int,
    wait_ms: int,
    debug_url: str = DEFAULT_CHROME_DEBUG_URL,
    url_validator: UrlValidator | None = None,
) -> BrowserPageSnapshot:
    base_url = normalize_debug_url(debug_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), trust_env=False) as client:
            version = await client.get(f"{base_url}/json/version")
            version.raise_for_status()
            target = await _open_target(client, base_url, "about:blank")
            try:
                return await _read_target_page(
                    target,
                    url=url,
                    max_chars=max_chars,
                    wait_ms=wait_ms,
                    url_validator=url_validator,
                )
            finally:
                await _close_target(client, base_url, target.id)
    except (httpx.HTTPError, ValueError) as exc:
        raise BrowserCdpError(f"Chrome DevTools endpoint is unavailable: {exc}") from exc


async def scroll_chrome_page(
    *,
    url: str,
    direction: str,
    pixels: int,
    passes: int,
    max_chars: int,
    wait_ms: int,
    debug_url: str = DEFAULT_CHROME_DEBUG_URL,
    url_validator: UrlValidator | None = None,
) -> BrowserActionResult:
    base_url = normalize_debug_url(debug_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), trust_env=False) as client:
            version = await client.get(f"{base_url}/json/version")
            version.raise_for_status()
            target = await _open_target(client, base_url, "about:blank")
            try:
                return await _scroll_target_page(
                    target,
                    url=url,
                    direction=direction,
                    pixels=pixels,
                    passes=passes,
                    max_chars=max_chars,
                    wait_ms=wait_ms,
                    url_validator=url_validator,
                )
            finally:
                await _close_target(client, base_url, target.id)
    except (httpx.HTTPError, ValueError) as exc:
        raise BrowserCdpError(f"Chrome DevTools endpoint is unavailable: {exc}") from exc


async def run_chrome_action(
    *,
    url: str,
    action: str,
    selector: str = "",
    target: str = "",
    text: str = "",
    value: str = "",
    max_chars: int,
    wait_ms: int,
    allow_sensitive: bool = False,
    debug_url: str = DEFAULT_CHROME_DEBUG_URL,
    url_validator: UrlValidator | None = None,
) -> BrowserActionResult:
    base_url = normalize_debug_url(debug_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), trust_env=False) as client:
            version = await client.get(f"{base_url}/json/version")
            version.raise_for_status()
            target_info = await _open_target(client, base_url, "about:blank")
            try:
                return await _run_target_action(
                    target_info,
                    url=url,
                    action=action,
                    selector=selector,
                    target=target,
                    text=text,
                    value=value,
                    max_chars=max_chars,
                    wait_ms=wait_ms,
                    allow_sensitive=allow_sensitive,
                    url_validator=url_validator,
                )
            finally:
                await _close_target(client, base_url, target_info.id)
    except (httpx.HTTPError, ValueError) as exc:
        raise BrowserCdpError(f"Chrome DevTools endpoint is unavailable: {exc}") from exc


async def _read_target_page(
    target: BrowserTarget,
    *,
    url: str,
    max_chars: int,
    wait_ms: int,
    url_validator: UrlValidator | None,
) -> BrowserPageSnapshot:
    try:
        async with websockets.connect(
            target.web_socket_debugger_url,
            max_size=4_000_000,
            open_timeout=8,
            close_timeout=2,
        ) as websocket:
            cdp = _CdpConnection(websocket, url_validator=url_validator)
            await _prepare_page(cdp)
            await cdp.send(
                "Page.navigate",
                {"url": await _validated_url_async(url, url_validator)},
            )
            ready_state = await _wait_for_ready_state(cdp, wait_ms)
            return await _snapshot_page(
                cdp,
                max_chars=max_chars,
                ready_state=ready_state,
                url_validator=url_validator,
            )
    except (OSError, WebSocketException, TimeoutError) as exc:
        raise BrowserCdpError(f"Chrome DevTools websocket failed: {exc}") from exc


async def _scroll_target_page(
    target: BrowserTarget,
    *,
    url: str,
    direction: str,
    pixels: int,
    passes: int,
    max_chars: int,
    wait_ms: int,
    url_validator: UrlValidator | None,
) -> BrowserActionResult:
    try:
        async with websockets.connect(
            target.web_socket_debugger_url,
            max_size=8_000_000,
            open_timeout=8,
            close_timeout=2,
        ) as websocket:
            cdp = _CdpConnection(websocket, url_validator=url_validator)
            await _prepare_page(cdp)
            await cdp.send(
                "Page.navigate",
                {"url": await _validated_url_async(url, url_validator)},
            )
            ready_state = await _wait_for_ready_state(cdp, wait_ms)
            scroll_payload = await _run_scroll_expression(
                cdp,
                direction=direction,
                pixels=pixels,
                passes=passes,
            )
            ready_state = await _wait_for_ready_state(cdp, min(wait_ms, 3000))
            snapshot = await _snapshot_page(
                cdp,
                max_chars=max_chars,
                ready_state=ready_state,
                url_validator=url_validator,
            )
            return BrowserActionResult(
                action="scroll",
                url=snapshot.url,
                title=snapshot.title,
                ready_state=snapshot.ready_state,
                ok=bool(scroll_payload.get("ok", True)),
                summary=str(scroll_payload.get("summary") or "Scrolled page."),
                snapshot=snapshot,
                target_info=scroll_payload,
            )
    except (OSError, WebSocketException, TimeoutError) as exc:
        raise BrowserCdpError(f"Chrome DevTools websocket failed: {exc}") from exc


async def _run_target_action(
    target_info: BrowserTarget,
    *,
    url: str,
    action: str,
    selector: str,
    target: str,
    text: str,
    value: str,
    max_chars: int,
    wait_ms: int,
    allow_sensitive: bool,
    url_validator: UrlValidator | None,
) -> BrowserActionResult:
    try:
        async with websockets.connect(
            target_info.web_socket_debugger_url,
            max_size=8_000_000,
            open_timeout=8,
            close_timeout=2,
        ) as websocket:
            cdp = _CdpConnection(websocket, url_validator=url_validator)
            await _prepare_page(cdp)
            await cdp.send(
                "Page.navigate",
                {"url": await _validated_url_async(url, url_validator)},
            )
            ready_state = await _wait_for_ready_state(cdp, wait_ms)
            action_payload = await _run_action_expression(
                cdp,
                action=action,
                selector=selector,
                target=target,
                text=text,
                value=value,
                allow_sensitive=allow_sensitive,
            )
            await asyncio.sleep(0.35)
            ready_state = await _wait_for_ready_state(cdp, min(wait_ms, 3000))
            snapshot = await _snapshot_page(
                cdp,
                max_chars=max_chars,
                ready_state=ready_state,
                url_validator=url_validator,
            )
            screenshot = None
            if action == "screenshot":
                screenshot = await _capture_screenshot(cdp)
            return BrowserActionResult(
                action=action,
                url=snapshot.url,
                title=snapshot.title,
                ready_state=snapshot.ready_state,
                ok=bool(action_payload.get("ok", True)),
                summary=str(action_payload.get("summary") or f"Browser action {action} finished."),
                snapshot=snapshot,
                screenshot_png=screenshot,
                selector=str(action_payload.get("selector") or selector),
                target=target,
                target_info=action_payload,
            )
    except (OSError, WebSocketException, TimeoutError) as exc:
        raise BrowserCdpError(f"Chrome DevTools websocket failed: {exc}") from exc


def normalize_debug_url(debug_url: str | None) -> str:
    raw = (debug_url or DEFAULT_CHROME_DEBUG_URL).strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme != "http":
        raise BrowserCdpError("Chrome DevTools URL must use http.")
    if not parsed.hostname or parsed.hostname.lower() not in LOCAL_DEBUG_HOSTS:
        raise BrowserCdpError("Chrome DevTools URL must point to localhost.")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise BrowserCdpError("Chrome DevTools URL must be a bare local origin.")
    return parsed.geturl().rstrip("/")


async def _open_target(
    client: httpx.AsyncClient,
    base_url: str,
    url: str,
) -> BrowserTarget:
    encoded_url = quote(url, safe="")
    response = await client.put(f"{base_url}/json/new?{encoded_url}")
    if response.status_code in {404, 405}:
        response = await client.get(f"{base_url}/json/new?{encoded_url}")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise BrowserCdpError("Chrome DevTools returned an invalid target payload.")
    target_id = str(payload.get("id") or "")
    websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
    if not target_id or not websocket_url:
        if target_id:
            await _close_target(client, base_url, target_id)
        raise BrowserCdpError("Chrome target has no websocket debugger URL.")
    try:
        _validate_websocket_debugger_url(websocket_url)
    except BrowserCdpError:
        await _close_target(client, base_url, target_id)
        raise
    return BrowserTarget(
        id=target_id,
        url=str(payload.get("url") or url),
        web_socket_debugger_url=websocket_url,
    )


async def _close_target(client: httpx.AsyncClient, base_url: str, target_id: str) -> None:
    if not target_id:
        return
    try:
        response = await client.get(f"{base_url}/json/close/{quote(target_id, safe='')}")
        if response.status_code not in {200, 404}:
            response.raise_for_status()
    except (httpx.HTTPError, OSError, ValueError):
        # Closing is best-effort and must never mask the action result or original error.
        return


def _validate_websocket_debugger_url(websocket_url: str) -> None:
    parsed = urlparse(websocket_url)
    if parsed.scheme not in {"ws", "wss"}:
        raise BrowserCdpError("Chrome target returned an invalid websocket debugger URL.")
    if not parsed.hostname or parsed.hostname.lower() not in LOCAL_DEBUG_HOSTS:
        raise BrowserCdpError("Chrome target websocket must point to localhost.")
    if parsed.username or parsed.password:
        raise BrowserCdpError("Chrome target websocket cannot contain credentials.")


def _validated_url(url: str, validator: UrlValidator | None) -> str:
    if validator is None:
        return url
    try:
        validated = validator(url)
    except BrowserCdpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BrowserCdpError(f"Blocked browser URL {url!r}: {exc}") from exc
    return str(validated or url)


async def _validated_url_async(url: str, validator: UrlValidator | None) -> str:
    if validator is None:
        return url
    return await asyncio.to_thread(_validated_url, url, validator)


async def _prepare_page(cdp: _CdpConnection) -> None:
    await cdp.send("Page.enable")
    await cdp.send("Runtime.enable")
    if cdp.url_validator is not None:
        await cdp.send("Network.enable")
        # WebSocket handshakes cannot be safely DNS-pinned per hop in an existing
        # operator Chrome session, so content tools fail closed on ws/wss.
        await cdp.send("Network.setBlockedURLs", {"urls": ["ws://*", "wss://*"]})
        await cdp.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "http://*/*", "requestStage": "Request"},
                    {"urlPattern": "https://*/*", "requestStage": "Request"},
                    {"urlPattern": "ws://*", "requestStage": "Request"},
                    {"urlPattern": "wss://*", "requestStage": "Request"},
                ]
            },
        )
        # Pause related targets before their first script/network action. Popups
        # are closed by _handle_attached_target; workers are closed as an
        # isolation trade-off rather than inheriting an unguarded network stack.
        await cdp.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )


class _CdpConnection:
    def __init__(self, websocket: Any, *, url_validator: UrlValidator | None = None) -> None:
        self.websocket = websocket
        self.url_validator = url_validator
        self._next_id = 0
        self._blocked_requests: list[dict[str, str]] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        message: dict[str, Any] = {"id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        await self.websocket.send(json.dumps(message, ensure_ascii=False))

        while True:
            raw = await asyncio.wait_for(self.websocket.recv(), timeout=timeout)
            data = json.loads(raw)
            event_method = str(data.get("method") or "")
            if event_method == "Fetch.requestPaused":
                await self._handle_request_paused(data.get("params"))
                continue
            if event_method in {
                "Network.webSocketCreated",
                "Network.webSocketWillSendHandshakeRequest",
            }:
                self._handle_websocket_event(data.get("params"))
                continue
            if event_method == "Target.attachedToTarget":
                await self._handle_attached_target(data.get("params"))
                continue
            if data.get("id") != message_id:
                continue
            if "error" in data:
                error = data["error"]
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("code") or error
                else:
                    detail = error
                raise BrowserCdpError(f"CDP command {method} failed: {detail}")
            result = data.get("result") or {}
            self.raise_for_blocked_requests()
            if not isinstance(result, dict):
                return {}
            return result

    async def _handle_request_paused(self, raw_params: Any) -> None:
        params = raw_params if isinstance(raw_params, dict) else {}
        request = params.get("request") if isinstance(params.get("request"), dict) else {}
        request_id = str(params.get("requestId") or "")
        url = str(request.get("url") or "")
        resource_type = str(params.get("resourceType") or "Other")
        try:
            await _validated_url_async(url, self.url_validator)
        except BrowserCdpError as exc:
            self._blocked_requests.append(
                {"url": url, "resource_type": resource_type, "reason": str(exc)}
            )
            await self._send_untracked(
                "Fetch.failRequest",
                {"requestId": request_id, "errorReason": "BlockedByClient"},
            )
            return
        await self._send_untracked("Fetch.continueRequest", {"requestId": request_id})

    def _handle_websocket_event(self, raw_params: Any) -> None:
        params = raw_params if isinstance(raw_params, dict) else {}
        request = params.get("request") if isinstance(params.get("request"), dict) else {}
        url = str(params.get("url") or request.get("url") or "")
        self._blocked_requests.append(
            {
                "url": url,
                "resource_type": "WebSocket",
                "reason": "WebSocket transport is disabled for isolated browser content tools.",
            }
        )

    async def _handle_attached_target(self, raw_params: Any) -> None:
        params = raw_params if isinstance(raw_params, dict) else {}
        target_info = (
            params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
        )
        target_id = str(target_info.get("targetId") or "")
        target_type = str(target_info.get("type") or "unknown")
        url = str(target_info.get("url") or "")
        if target_id:
            await self._send_untracked("Target.closeTarget", {"targetId": target_id})
        if target_type in {"page", "webview"}:
            self._blocked_requests.append(
                {
                    "url": url,
                    "resource_type": "Popup",
                    "reason": "New browser targets are disabled for isolated content tools.",
                }
            )

    async def _send_untracked(self, method: str, params: dict[str, Any]) -> None:
        self._next_id += 1
        await self.websocket.send(
            json.dumps(
                {"id": self._next_id, "method": method, "params": params},
                ensure_ascii=False,
            )
        )

    def raise_for_blocked_requests(self) -> None:
        if not self._blocked_requests:
            return
        first = self._blocked_requests[0]
        raise BrowserCdpError(
            "Blocked CDP request "
            f"({first['resource_type']}) to {first['url']!r}: {first['reason']}"
        )


async def _wait_for_ready_state(cdp: _CdpConnection, wait_ms: int) -> str:
    deadline = asyncio.get_running_loop().time() + max(wait_ms, 250) / 1000
    ready_state = "unknown"
    while asyncio.get_running_loop().time() < deadline:
        ready_state = await _document_ready_state(cdp)
        if ready_state == "complete":
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(min(0.5, remaining))
            return ready_state
        await asyncio.sleep(0.25)
    return ready_state


async def _document_ready_state(cdp: _CdpConnection) -> str:
    result = await cdp.send(
        "Runtime.evaluate",
        {"expression": "document.readyState", "returnByValue": True},
    )
    value = (result.get("result") or {}).get("value")
    return str(value or "unknown")


async def _snapshot_page(
    cdp: _CdpConnection,
    *,
    max_chars: int,
    ready_state: str,
    url_validator: UrlValidator | None = None,
) -> BrowserPageSnapshot:
    expression = _snapshot_expression(max_chars + 1)
    result = await cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        timeout=12.0,
    )
    value = (result.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise BrowserCdpError("Chrome page snapshot returned no serializable value.")
    text = str(value.get("text") or "")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    title = str(value.get("title") or "")
    final_url = str(value.get("url") or "")
    await _validated_url_async(final_url, url_validator)
    page_ready_state = str(value.get("readyState") or ready_state)
    return BrowserPageSnapshot(
        title=title,
        url=final_url,
        ready_state=page_ready_state,
        text=text,
        truncated=truncated,
        needs_human_verification=looks_like_human_verification(title, text),
        form_count=_int_value(value.get("formCount")),
        password_input_count=_int_value(value.get("passwordInputCount")),
        sensitive_input_count=_int_value(value.get("sensitiveInputCount")),
    )


async def _run_action_expression(
    cdp: _CdpConnection,
    *,
    action: str,
    selector: str,
    target: str,
    text: str,
    value: str,
    allow_sensitive: bool,
) -> dict[str, Any]:
    if action == "screenshot":
        return {"ok": True, "summary": "Screenshot captured."}
    expression = _action_expression(
        action=action,
        selector=selector,
        target=target,
        text=text,
        value=value,
        allow_sensitive=allow_sensitive,
    )
    result = await cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        timeout=12.0,
    )
    value_payload = (result.get("result") or {}).get("value")
    if not isinstance(value_payload, dict):
        raise BrowserCdpError("Chrome action returned no serializable value.")
    return value_payload


async def _capture_screenshot(cdp: _CdpConnection) -> bytes:
    result = await cdp.send(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": False},
        timeout=12.0,
    )
    data = result.get("data")
    if not isinstance(data, str) or not data:
        raise BrowserCdpError("Chrome screenshot returned no image data.")
    return base64.b64decode(data)


async def _run_scroll_expression(
    cdp: _CdpConnection,
    *,
    direction: str,
    pixels: int,
    passes: int,
) -> dict[str, Any]:
    expression = _scroll_expression(direction=direction, pixels=pixels, passes=passes)
    result = await cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        timeout=20.0,
    )
    value_payload = (result.get("result") or {}).get("value")
    if not isinstance(value_payload, dict):
        raise BrowserCdpError("Chrome scroll returned no serializable value.")
    return value_payload


def _scroll_expression(*, direction: str, pixels: int, passes: int) -> str:
    direction_json = json.dumps(direction)
    pixels_json = json.dumps(max(100, int(pixels)))
    passes_json = json.dumps(max(1, int(passes)))
    return f"""(async () => {{
  const direction = {direction_json};
  const pixels = {pixels_json};
  const passes = {passes_json};
  const before = {{
    x: window.scrollX || 0,
    y: window.scrollY || 0,
    height: Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0),
  }};
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  if (direction === "top") {{
    window.scrollTo({{ top: 0, behavior: "instant" }});
    await wait(250);
  }} else if (direction === "bottom" || direction === "end") {{
    for (let index = 0; index < passes; index += 1) {{
      const height = Math.max(
        document.body?.scrollHeight || 0,
        document.documentElement?.scrollHeight || 0
      );
      window.scrollTo({{ top: height, behavior: "instant" }});
      await wait(450);
    }}
  }} else {{
    const delta = direction === "up" ? -pixels : pixels;
    for (let index = 0; index < passes; index += 1) {{
      window.scrollBy({{ top: delta, left: 0, behavior: "instant" }});
      await wait(450);
    }}
  }}
  const after = {{
    x: window.scrollX || 0,
    y: window.scrollY || 0,
    height: Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0),
  }};
  return {{
    ok: true,
    summary: `Scrolled ${{direction}} ${{passes}} pass(es).`,
    direction,
    pixels,
    passes,
    before,
    after,
    heightChanged: after.height !== before.height,
  }};
}})()"""


def _action_expression(
    *,
    action: str,
    selector: str,
    target: str,
    text: str,
    value: str,
    allow_sensitive: bool,
) -> str:
    action_json = json.dumps(action)
    selector_json = json.dumps(selector)
    target_json = json.dumps(target)
    text_json = json.dumps(text)
    value_json = json.dumps(value)
    allow_sensitive_json = "true" if allow_sensitive else "false"
    return f"""(() => {{
  const action = {action_json};
  const selector = {selector_json};
  const targetQuery = {target_json};
  const text = {text_json};
  const value = {value_json};
  const allowSensitive = {allow_sensitive_json};
  const dangerousWords =
    /pay|purchase|buy|order|delete|remove|submit|confirm|transfer|checkout/i;
  const sensitiveWords = /password|passcode|otp|token|secret|card|cc-|credit|cvv|cvc|ssn|passport/i;
  const event = (name) => new Event(name, {{ bubbles: true, cancelable: true }});
  const normalize = (value) => String(value || "")
    .toLowerCase()
    .replace(/[^a-zа-яё0-9]+/gi, " ")
    .trim();
  const isVisible = (element) => {{
    if (!element || element.disabled) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || "1") > 0 && rect.width > 0 && rect.height > 0;
  }};
  const cssPath = (element) => {{
    if (!element) return "";
    if (element.id && /^[A-Za-z][\\w:-]*$/.test(element.id)) return `#${{element.id}}`;
    const parts = [];
    let current = element;
    while (current && current.nodeType === 1 && current !== document.body) {{
      const tag = current.tagName.toLowerCase();
      const siblings = Array.from(current.parentElement?.children || [])
        .filter((item) => item.tagName === current.tagName);
      const index = siblings.indexOf(current) + 1;
      parts.unshift(siblings.length > 1 ? `${{tag}}:nth-of-type(${{index}})` : tag);
      current = current.parentElement;
      if (parts.length >= 6) break;
    }}
    return parts.join(" > ");
  }};
  const labelFor = (element) => {{
    const id = element.id || "";
    const labels = [];
    if (id) {{
      document.querySelectorAll(`label[for="${{CSS.escape(id)}}"]`).forEach((label) => {{
        labels.push(label.innerText || "");
      }});
    }}
    if (element.labels) {{
      Array.from(element.labels).forEach((label) => labels.push(label.innerText || ""));
    }}
    return labels.join(" ");
  }};
  const textFor = (element) => [
    element.innerText || element.value || "",
    element.getAttribute("aria-label") || "",
    element.getAttribute("title") || "",
    element.getAttribute("placeholder") || "",
    element.getAttribute("name") || "",
    element.getAttribute("id") || "",
    element.getAttribute("role") || "",
    labelFor(element),
  ].join(" ").trim();
  const scoreElement = (element, query) => {{
    const normalized = normalize(textFor(element));
    const wanted = normalize(query);
    if (!wanted || !normalized) return 0;
    let score = normalized.includes(wanted) ? 20 : 0;
    for (const token of wanted.split(" ").filter(Boolean)) {{
      if (normalized.includes(token)) score += 2;
    }}
    const role = normalize(element.getAttribute("role") || element.tagName);
    if (wanted.includes(role)) score += 3;
    return score;
  }};
  const findTarget = () => {{
    if (selector) return document.querySelector(selector);
    if (!targetQuery) return null;
    const selectorPool = action === "type"
      ? "input, textarea, [contenteditable=true]"
      : action === "select"
        ? "select, [role=listbox], input"
        : [
            "button",
            "a",
            "input",
            "textarea",
            "select",
            "[role=button]",
            "[role=link]",
            "[aria-label]",
            "[title]",
            "label",
          ].join(", ");
    let best = null;
    for (const element of Array.from(document.querySelectorAll(selectorPool))) {{
      if (!isVisible(element)) continue;
      const score = scoreElement(element, targetQuery);
      if (score > 0 && (!best || score > best.score)) {{
        best = {{ element, score }};
      }}
    }}
    return best ? best.element : null;
  }};
  const target = action === "screenshot" ? null : findTarget();
  if (action !== "screenshot" && !target) {{
    const missing = selector
      ? `Selector not found: ${{selector}}`
      : `Target not found: ${{targetQuery}}`;
    return {{ ok: false, summary: missing, selector, target: targetQuery }};
  }}
  const hint = target
    ? [
        target.type || "",
        target.name || "",
        target.id || "",
        target.autocomplete || "",
      ].join(" ")
    : "";
  const label = target
    ? [
        target.innerText || target.value || "",
        target.getAttribute("aria-label") || "",
        target.title || "",
      ].join(" ").trim()
    : "";
  const sensitive = target && sensitiveWords.test(hint);
  const dangerous = target && dangerousWords.test(`${{hint}} ${{label}}`);
  if (sensitive && !allowSensitive) {{
    return {{
      ok: false,
      summary: "Target looks sensitive; set allow_sensitive only after operator approval.",
      sensitive,
      dangerous,
    }};
  }}
  if (action === "click") {{
    target.scrollIntoView({{ block: "center", inline: "center" }});
    target.click();
    return {{
      ok: true,
      summary: dangerous ? "Clicked target with dangerous-word warning." : "Clicked target.",
      sensitive,
      dangerous,
      label: label.slice(0, 160),
      selector: cssPath(target),
      target: targetQuery,
    }};
  }}
  if (action === "type") {{
    target.focus();
    if ("value" in target) {{
      target.value = text;
    }} else {{
      target.textContent = text;
    }}
    target.dispatchEvent(event("input"));
    target.dispatchEvent(event("change"));
    return {{
      ok: true,
      summary: "Typed text into target.",
      sensitive,
      dangerous,
      selector: cssPath(target),
      target: targetQuery,
    }};
  }}
  if (action === "select") {{
    target.focus();
    const wanted = normalize(value || text);
    if (target.tagName && target.tagName.toLowerCase() === "select") {{
      const option = Array.from(target.options || []).find((item) =>
        normalize(item.value) === wanted || normalize(item.textContent).includes(wanted)
      );
      target.value = option ? option.value : (value || text);
    }} else {{
      target.value = value || text;
    }}
    target.dispatchEvent(event("input"));
    target.dispatchEvent(event("change"));
    return {{
      ok: true,
      summary: "Selected value on target.",
      sensitive,
      dangerous,
      selector: cssPath(target),
      target: targetQuery,
    }};
  }}
  return {{ ok: false, summary: `Unsupported browser action: ${{action}}` }};
}})()"""


def _snapshot_expression(limit: int) -> str:
    return f"""(() => {{
  const limit = {int(limit)};
  const raw = document.body ? (document.body.innerText || "") : "";
  const inputs = Array.from(document.querySelectorAll("input, textarea"));
  const sensitive = inputs.filter((input) => {{
    const type = String(input.getAttribute("type") || "").toLowerCase();
    const name = String(input.getAttribute("name") || "").toLowerCase();
    const id = String(input.getAttribute("id") || "").toLowerCase();
    const autocomplete = String(input.getAttribute("autocomplete") || "").toLowerCase();
    const hint = `${{type}} ${{name}} ${{id}} ${{autocomplete}}`;
    return /password|passcode|otp|token|secret|card|cc-|credit|cvv|cvc|ssn|passport/.test(hint);
  }});
  const text = raw
    .replace(/\\r/g, "")
    .replace(/[ \\t]+\\n/g, "\\n")
    .replace(/\\n{{3,}}/g, "\\n\\n")
    .trim()
    .slice(0, limit);
  return {{
    title: document.title || "",
    url: location.href,
    readyState: document.readyState,
    text,
    formCount: document.forms ? document.forms.length : 0,
    passwordInputCount: inputs.filter((input) =>
      String(input.getAttribute("type") || "").toLowerCase() === "password"
    ).length,
    sensitiveInputCount: sensitive.length
  }};
}})()"""


def looks_like_human_verification(title: str, text: str) -> bool:
    haystack = f"{title}\n{text[:2000]}".lower()
    return any(marker in haystack for marker in HUMAN_VERIFICATION_MARKERS)


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
