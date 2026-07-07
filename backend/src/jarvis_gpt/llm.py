from __future__ import annotations

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
            async with httpx.AsyncClient(timeout=3.0) as client:
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
        max_tokens: int = 1200,
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
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
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


def _exc_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__
