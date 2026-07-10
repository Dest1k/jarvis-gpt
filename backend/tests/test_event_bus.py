from __future__ import annotations

import asyncio
import json

from jarvis_gpt import event_bus as event_bus_module
from jarvis_gpt.event_bus import EventBus


class _FakeWebSocket:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.accepted = False
        self.messages: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        if self.blocked:
            await asyncio.Future()
        self.messages.append(data)


def test_publish_does_not_serialize_slow_websocket_clients(monkeypatch):
    monkeypatch.setattr(event_bus_module, "EVENT_SEND_TIMEOUT_SEC", 0.01)
    bus = EventBus()
    slow = _FakeWebSocket(blocked=True)
    healthy = _FakeWebSocket()

    async def scenario() -> None:
        await bus.connect(slow)  # type: ignore[arg-type]
        await bus.connect(healthy)  # type: ignore[arg-type]
        await asyncio.wait_for(bus.publish({"kind": "heartbeat"}), timeout=0.2)

    asyncio.run(scenario())

    assert json.loads(healthy.messages[0]) == {"kind": "heartbeat"}
    assert slow not in bus._clients
    assert healthy in bus._clients
