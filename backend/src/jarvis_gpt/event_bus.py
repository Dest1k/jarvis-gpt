from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

EVENT_SEND_TIMEOUT_SEC = 2.0


class EventBus:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def publish(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False)
        async with self._lock:
            clients = list(self._clients)

        async def send(client: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(
                    client.send_text(data),
                    timeout=EVENT_SEND_TIMEOUT_SEC,
                )
                return None
            except Exception:  # noqa: BLE001
                return client

        dead = [
            client
            for client in await asyncio.gather(*(send(client) for client in clients))
            if client is not None
        ]
        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)
