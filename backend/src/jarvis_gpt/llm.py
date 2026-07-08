from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from .config import JarvisSettings
from .model_catalog import ModelCatalog


@dataclass(frozen=True)
class LLMResult:
    ok: bool
    content: str
    error: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMStreamChunk:
    kind: str
    content: str = ""
    error: str | None = None
    raw: dict[str, Any] | None = None


class LLMRouter:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    async def health(self) -> dict[str, Any]:
        local = ModelCatalog(self.settings).response()
        if not self.settings.llm_enabled:
            return {
                "ok": False,
                "disabled": True,
                "message": "LLM router is disabled",
                "local": local,
            }
        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                response = await client.get(f"{self.settings.llm_base_url}/models")
                response.raise_for_status()
            data = response.json()
            served = [
                item.get("id")
                for item in data.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
            return {
                "ok": True,
                "status_code": response.status_code,
                "served_models": served,
                "configured_model": self.settings.llm_model,
                "local": local,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _exc_message(exc), "local": local}

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        if not self.settings.llm_enabled:
            return LLMResult(ok=False, content="", error="LLM router is disabled")

        request_temperature = (
            self.settings.profile.temperature if temperature is None else temperature
        )
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": request_temperature,
            "max_tokens": self.settings.llm_max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        try:
            timeout = httpx.Timeout(self.settings.llm_timeout_sec, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.settings.llm_base_url}/chat/completions",
                    json=body,
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001
            return LLMResult(ok=False, content="", error=_exc_message(exc))

        choices = data.get("choices") or []
        if not choices:
            return LLMResult(ok=False, content="", error="LLM response has no choices", raw=data)
        content = (choices[0].get("message") or {}).get("content") or ""
        return LLMResult(ok=True, content=content.strip(), raw=data)

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        if not self.settings.llm_enabled:
            yield LLMStreamChunk(kind="error", error="LLM router is disabled")
            return

        request_temperature = (
            self.settings.profile.temperature if temperature is None else temperature
        )
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": request_temperature,
            "max_tokens": self.settings.llm_max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        try:
            timeout = httpx.Timeout(self.settings.llm_timeout_sec, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client, client.stream(
                "POST",
                f"{self.settings.llm_base_url}/chat/completions",
                json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    chunk = _stream_chunk_from_line(line)
                    if chunk is None:
                        continue
                    if chunk.kind == "done":
                        return
                    yield chunk
        except Exception as exc:  # noqa: BLE001
            yield LLMStreamChunk(kind="error", error=_exc_message(exc))


def _exc_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _stream_chunk_from_line(line: str) -> LLMStreamChunk | None:
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line.removeprefix("data:").strip()
    if line == "[DONE]":
        return LLMStreamChunk(kind="done")
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    choices = data.get("choices") or []
    if not choices:
        return None
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content") or choice.get("text") or ""
    if content:
        return LLMStreamChunk(kind="delta", content=content, raw=data)
    if choice.get("finish_reason"):
        return LLMStreamChunk(kind="done", raw=data)
    return None
