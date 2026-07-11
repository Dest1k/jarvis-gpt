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
    def __init__(self) -> None:
        _record("construct")

    async def close(self) -> None:
        _record("close")

    async def fast_fact(self, query: str) -> dict[str, str]:
        return {"query": query}

    def deep_research(self, query: str) -> dict[str, str]:
        return {"query": query}

    async def aggressive_shopping(self, product_url: str) -> dict[str, str]:
        return {"product_url": product_url}
