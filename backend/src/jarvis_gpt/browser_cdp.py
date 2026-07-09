from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import websockets
from websockets.exceptions import WebSocketException

DEFAULT_CHROME_DEBUG_URL = "http://127.0.0.1:9222"
LOCAL_DEBUG_HOSTS = {"127.0.0.1", "localhost", "::1"}

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
    pass


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
) -> BrowserPageSnapshot:
    base_url = normalize_debug_url(debug_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), trust_env=False) as client:
            version = await client.get(f"{base_url}/json/version")
            version.raise_for_status()
            target = await _open_target(client, base_url, url)
    except (httpx.HTTPError, ValueError) as exc:
        raise BrowserCdpError(f"Chrome DevTools endpoint is unavailable: {exc}") from exc

    try:
        async with websockets.connect(
            target.web_socket_debugger_url,
            max_size=4_000_000,
            open_timeout=8,
            close_timeout=2,
        ) as websocket:
            cdp = _CdpConnection(websocket)
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.navigate", {"url": url})
            ready_state = await _wait_for_ready_state(cdp, wait_ms)
            return await _snapshot_page(cdp, max_chars=max_chars, ready_state=ready_state)
    except (OSError, WebSocketException, TimeoutError) as exc:
        raise BrowserCdpError(f"Chrome DevTools websocket failed: {exc}") from exc


async def run_chrome_action(
    *,
    url: str,
    action: str,
    selector: str = "",
    text: str = "",
    value: str = "",
    max_chars: int,
    wait_ms: int,
    allow_sensitive: bool = False,
    debug_url: str = DEFAULT_CHROME_DEBUG_URL,
) -> BrowserActionResult:
    base_url = normalize_debug_url(debug_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), trust_env=False) as client:
            version = await client.get(f"{base_url}/json/version")
            version.raise_for_status()
            target = await _open_target(client, base_url, url)
    except (httpx.HTTPError, ValueError) as exc:
        raise BrowserCdpError(f"Chrome DevTools endpoint is unavailable: {exc}") from exc

    try:
        async with websockets.connect(
            target.web_socket_debugger_url,
            max_size=8_000_000,
            open_timeout=8,
            close_timeout=2,
        ) as websocket:
            cdp = _CdpConnection(websocket)
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.navigate", {"url": url})
            ready_state = await _wait_for_ready_state(cdp, wait_ms)
            action_payload = await _run_action_expression(
                cdp,
                action=action,
                selector=selector,
                text=text,
                value=value,
                allow_sensitive=allow_sensitive,
            )
            await asyncio.sleep(0.35)
            ready_state = await _wait_for_ready_state(cdp, min(wait_ms, 3000))
            snapshot = await _snapshot_page(cdp, max_chars=max_chars, ready_state=ready_state)
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
    websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
    if not websocket_url:
        raise BrowserCdpError("Chrome target has no websocket debugger URL.")
    return BrowserTarget(
        id=str(payload.get("id") or ""),
        url=str(payload.get("url") or url),
        web_socket_debugger_url=websocket_url,
    )


class _CdpConnection:
    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self._next_id = 0

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
            if not isinstance(result, dict):
                return {}
            return result


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
    text: str,
    value: str,
    allow_sensitive: bool,
) -> dict[str, Any]:
    if action == "screenshot":
        return {"ok": True, "summary": "Screenshot captured."}
    expression = _action_expression(
        action=action,
        selector=selector,
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


def _action_expression(
    *,
    action: str,
    selector: str,
    text: str,
    value: str,
    allow_sensitive: bool,
) -> str:
    action_json = json.dumps(action)
    selector_json = json.dumps(selector)
    text_json = json.dumps(text)
    value_json = json.dumps(value)
    allow_sensitive_json = "true" if allow_sensitive else "false"
    return f"""(() => {{
  const action = {action_json};
  const selector = {selector_json};
  const text = {text_json};
  const value = {value_json};
  const allowSensitive = {allow_sensitive_json};
  const dangerousWords =
    /pay|purchase|buy|order|delete|remove|submit|confirm|transfer|checkout/i;
  const sensitiveWords = /password|passcode|otp|token|secret|card|cc-|credit|cvv|cvc|ssn|passport/i;
  const event = (name) => new Event(name, {{ bubbles: true, cancelable: true }});
  const target = selector ? document.querySelector(selector) : null;
  if (action !== "screenshot" && !target) {{
    return {{ ok: false, summary: `Selector not found: ${{selector}}` }};
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
    }};
  }}
  if (action === "type") {{
    target.focus();
    target.value = text;
    target.dispatchEvent(event("input"));
    target.dispatchEvent(event("change"));
    return {{ ok: true, summary: "Typed text into target.", sensitive, dangerous }};
  }}
  if (action === "select") {{
    target.focus();
    target.value = value || text;
    target.dispatchEvent(event("input"));
    target.dispatchEvent(event("change"));
    return {{ ok: true, summary: "Selected value on target.", sensitive, dangerous }};
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
