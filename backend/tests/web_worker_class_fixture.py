from __future__ import annotations

import os
from pathlib import Path


def _record(event: str) -> None:
    target = os.environ.get("JARVIS_WEB_CLASS_LIFECYCLE_PATH")
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(f"{event}:{os.getpid()}\n")


class JarvisWebSurfer:
    def __init__(
        self,
        *,
        proxies: list[str] | None = None,
        headless: bool | None = None,
    ) -> None:
        self.started = False
        self.proxy_count = len(proxies or [])
        self.headless = headless
        _record("construct")

    async def start(self) -> None:
        _record("start")
        if os.environ.get("JARVIS_WEB_CLASS_FAIL_START") == "1":
            raise RuntimeError("fixture startup failed")
        self.started = True

    async def close(self) -> None:
        _record("close")
        self.started = False

    async def fast_fact(self, query: str) -> dict[str, object]:
        assert self.started
        _record("fast_fact")
        return {
            "query": query,
            "worker_pid": os.getpid(),
            "proxy_count": self.proxy_count,
            "headless": self.headless,
        }

    async def deep_research(self, query: str, max_depth: int = 3) -> dict[str, object]:
        assert self.started
        _record("deep_research")
        return {"query": query, "max_depth": max_depth, "worker_pid": os.getpid()}

    async def aggressive_shopping(self, product_url: str) -> dict[str, object]:
        assert self.started
        _record("aggressive_shopping")
        return {"product_url": product_url, "worker_pid": os.getpid()}
