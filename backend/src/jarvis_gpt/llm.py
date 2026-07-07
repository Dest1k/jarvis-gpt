from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import JarvisSettings


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
        if not self.settings.llm_enabled:
            return {"ok": False, "disabled": True, "message": "LLM router is disabled"}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.settings.llm_base_url}/models")
                response.raise_for_status()
            return {"ok": True, "status_code": response.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

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
            return LLMResult(ok=False, content="", error=str(exc))

        choices = data.get("choices") or []
        if not choices:
            return LLMResult(ok=False, content="", error="LLM response has no choices", raw=data)
        content = (choices[0].get("message") or {}).get("content") or ""
        return LLMResult(ok=True, content=content.strip(), raw=data)
