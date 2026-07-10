from __future__ import annotations

import asyncio
import json

import pytest
from jarvis_gpt import browser_cdp
from jarvis_gpt.browser_cdp import (
    BrowserCdpError,
    BrowserTarget,
    _CdpConnection,
    _prepare_page,
)


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = [json.dumps(item) for item in messages]
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self.messages:
            raise AssertionError("Unexpected websocket receive")
        return self.messages.pop(0)


def test_cdp_fetch_guard_fails_private_subrequest_before_continuing():
    websocket = FakeWebSocket(
        [
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": "req-private",
                    "resourceType": "Document",
                    "request": {"url": "http://192.168.1.1/admin"},
                },
            },
            {"id": 1, "result": {}},
        ]
    )

    def public_only(url: str) -> str:
        if "192.168.1.1" in url:
            raise ValueError("private network")
        return url

    connection = _CdpConnection(websocket, url_validator=public_only)
    with pytest.raises(BrowserCdpError, match="192.168.1.1"):
        asyncio.run(connection.send("Runtime.evaluate", {"expression": "1"}))

    assert websocket.sent[1]["method"] == "Fetch.failRequest"
    assert websocket.sent[1]["params"]["requestId"] == "req-private"


def test_cdp_fails_closed_on_websocket_to_metadata_service():
    websocket = FakeWebSocket(
        [
            {
                "method": "Network.webSocketCreated",
                "params": {"url": "ws://169.254.169.254/latest/meta-data"},
            },
            {"id": 1, "result": {}},
        ]
    )
    connection = _CdpConnection(websocket, url_validator=lambda url: url)

    with pytest.raises(BrowserCdpError, match="WebSocket"):
        asyncio.run(connection.send("Runtime.evaluate", {"expression": "1"}))


def test_cdp_closes_new_popup_target_before_it_can_run():
    websocket = FakeWebSocket(
        [
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "targetInfo": {
                        "targetId": "popup-1",
                        "type": "page",
                        "url": "http://192.168.1.1/router",
                    }
                },
            },
            {"id": 1, "result": {}},
        ]
    )
    connection = _CdpConnection(websocket, url_validator=lambda url: url)

    with pytest.raises(BrowserCdpError, match="New browser targets"):
        asyncio.run(connection.send("Runtime.evaluate", {"expression": "1"}))

    close = websocket.sent[1]
    assert close["method"] == "Target.closeTarget"
    assert close["params"] == {"targetId": "popup-1"}


def test_prepare_page_intercepts_http_websocket_and_related_targets():
    class Recorder:
        @staticmethod
        def url_validator(url):
            return url

        def __init__(self):
            self.calls = []

        async def send(self, method, params=None):
            self.calls.append((method, params))
            return {}

    recorder = Recorder()
    asyncio.run(_prepare_page(recorder))

    calls = dict(recorder.calls)
    assert calls["Network.setBlockedURLs"] == {"urls": ["ws://*", "wss://*"]}
    patterns = calls["Fetch.enable"]["patterns"]
    assert {item["urlPattern"] for item in patterns} == {
        "http://*/*",
        "https://*/*",
        "ws://*",
        "wss://*",
    }
    assert calls["Target.setAutoAttach"]["waitForDebuggerOnStart"] is True


def test_read_chrome_page_always_closes_temporary_target(monkeypatch):
    closed: list[str] = []
    target = BrowserTarget(
        id="target-1",
        url="about:blank",
        web_socket_debugger_url="ws://127.0.0.1:9222/devtools/page/target-1",
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return FakeResponse()

    async def fake_open(_client, _base_url, _url):
        return target

    async def fake_read(_target, **_kwargs):
        raise BrowserCdpError("navigation failed")

    async def fake_close(_client, _base_url, target_id):
        closed.append(target_id)

    monkeypatch.setattr(browser_cdp.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(browser_cdp, "_open_target", fake_open)
    monkeypatch.setattr(browser_cdp, "_read_target_page", fake_read)
    monkeypatch.setattr(browser_cdp, "_close_target", fake_close)

    with pytest.raises(BrowserCdpError, match="navigation failed"):
        asyncio.run(
            browser_cdp.read_chrome_page(
                url="https://example.com",
                max_chars=1000,
                wait_ms=1000,
            )
        )

    assert closed == ["target-1"]
