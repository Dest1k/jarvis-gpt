from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

EVENT_SEND_TIMEOUT_SEC = 2.0


class EventBus:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, *, user_id: str) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients[websocket] = user_id

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(websocket, None)

    async def publish(self, event: dict[str, Any], *, user_id: str | None = None) -> None:
        # Import lazily to keep the event transport usable in isolation and avoid
        # turning its module import into an IAM/storage dependency cycle.
        if user_id is None:
            from .authorization import current_user_id

            user_id = current_user_id()
        data = json.dumps(event, ensure_ascii=False)
        async with self._lock:
            clients = [
                client
                for client, client_user_id in self._clients.items()
                if client_user_id == user_id
            ]

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
                    self._clients.pop(client, None)
