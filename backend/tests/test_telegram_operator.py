from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from jarvis_gpt.telegram_operator import (
    TelegramDeliveryError,
    send_telegram_text,
)


def test_operator_delivery_pins_bot_realm_and_sends_literal_text():
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, payload))
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 700001, "is_bot": True}},
            )
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 77,
                        "date": 1_784_714_402,
                        "from": {"id": 700001, "is_bot": True},
                    },
                },
            )
        raise AssertionError(request.url.path)

    result = asyncio.run(
        send_telegram_text(
            bot_token="700001:test-token",
            realm_id="telegram:700001",
            chat_id=424242,
            content="<b>literal text</b>",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result == {"bot_id": 700001, "message_id": 77, "date": 1_784_714_402}
    assert requests[1] == (
        "/bot700001:test-token/sendMessage",
        {
            "chat_id": 424242,
            "text": "<b>literal text</b>",
            "disable_web_page_preview": True,
        },
    )
    assert "parse_mode" not in requests[1][1]


def test_operator_delivery_rejects_wrong_bot_before_send():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 800002, "is_bot": True}},
        )

    with pytest.raises(TelegramDeliveryError, match="telegram_bot_realm_mismatch"):
        asyncio.run(
            send_telegram_text(
                bot_token="800002:test-token",
                realm_id="telegram:700001",
                chat_id=424242,
                content="must not be sent",
                transport=httpx.MockTransport(handler),
            )
        )

    assert paths == ["/bot800002:test-token/getMe"]


def test_operator_identity_5xx_is_definite_before_send():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(503, json={"ok": False})

    with pytest.raises(TelegramDeliveryError) as caught:
        asyncio.run(
            send_telegram_text(
                bot_token="700001:test-token",
                realm_id="telegram:700001",
                chat_id=424242,
                content="not sent",
                transport=httpx.MockTransport(handler),
            )
        )

    assert caught.value.code == "telegram_identity_503"
    assert caught.value.uncertain is False
    assert paths == ["/bot700001:test-token/getMe"]
