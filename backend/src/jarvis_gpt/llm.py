from __future__ import annotations

import asyncio
import json
import math
import statistics
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

import httpx

from .config import JarvisSettings, detect_repeated_token_degeneration
from .model_catalog import ModelCatalog


@dataclass(frozen=True)
class LLMResult:
    ok: bool
    content: str
    error: str | None = None
    raw: dict[str, Any] | None = None
    preempted: bool = False


@dataclass(frozen=True)
class LLMStreamChunk:
    kind: str
    content: str = ""
    error: str | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None
    preempted: bool = False


LLMPriority = Literal["foreground", "background"]
BACKGROUND_PREEMPTED_ERROR = "Background LLM request preempted by foreground traffic."
_T = TypeVar("_T")


class _BackgroundPreempted(RuntimeError):
    pass


@dataclass(eq=False)
class _AdmissionLease:
    priority: LLMPriority
    state: _LoopAdmissionState
    preempted: asyncio.Event = field(default_factory=asyncio.Event)
    released: bool = False


@dataclass
class _LoopAdmissionState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    foreground_active: int = 0
    foreground_waiting: int = 0
    background_active: int = 0
    background_waiting: int = 0
    active_background: set[_AdmissionLease] = field(default_factory=set)
    foreground_requests: int = 0
    background_requests: int = 0
    foreground_completed: int = 0
    background_completed: int = 0
    background_deferred: int = 0
    background_preemptions: int = 0


@contextmanager
def background_llm_priority(llm: Any) -> Iterator[None]:
    """Mark nested LLM calls as idle/background work when the router supports it."""

    factory = getattr(llm, "background_priority", None)
    if not callable(factory):
        yield
        return
    with factory():
        yield


class LLMRouter:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings
        self._priority: ContextVar[LLMPriority] = ContextVar(
            f"jarvis_llm_priority_{id(self)}",
            default="foreground",
        )
        # asyncio primitives are loop-bound after first use. Keep one admission
        # state per event loop so CLI/tests may safely reuse a router across
        # multiple asyncio.run() calls.
        self._admission_states: dict[asyncio.AbstractEventLoop, _LoopAdmissionState] = {}
        self._admission_states_guard = threading.Lock()

    @contextmanager
    def background_priority(self) -> Iterator[None]:
        """Make all nested requests preemptible background work."""

        token = self._priority.set("background")
        try:
            yield
        finally:
            self._priority.reset(token)

    def admission_status(self) -> dict[str, Any]:
        """Return aggregate foreground/background scheduling counters."""

        with self._admission_states_guard:
            states = list(self._admission_states.values())
        return {
            "priority": self._priority.get(),
            "foreground_active": sum(item.foreground_active for item in states),
            "foreground_waiting": sum(item.foreground_waiting for item in states),
            "background_active": sum(item.background_active for item in states),
            "background_waiting": sum(item.background_waiting for item in states),
            "foreground_requests": sum(item.foreground_requests for item in states),
            "background_requests": sum(item.background_requests for item in states),
            "foreground_completed": sum(item.foreground_completed for item in states),
            "background_completed": sum(item.background_completed for item in states),
            "background_deferred": sum(item.background_deferred for item in states),
            "background_preemptions": sum(item.background_preemptions for item in states),
        }

    def _loop_admission_state(self) -> _LoopAdmissionState:
        loop = asyncio.get_running_loop()
        with self._admission_states_guard:
            closed = [item for item in self._admission_states if item.is_closed()]
            for item in closed:
                self._admission_states.pop(item, None)
            state = self._admission_states.get(loop)
            if state is None:
                state = _LoopAdmissionState()
                self._admission_states[loop] = state
            return state

    async def _acquire_admission(self, priority: LLMPriority) -> _AdmissionLease:
        state = self._loop_admission_state()
        if priority == "foreground":
            async with state.condition:
                state.foreground_requests += 1
                state.foreground_waiting += 1
                try:
                    for background in tuple(state.active_background):
                        if background.preempted.is_set():
                            continue
                        background.preempted.set()
                        state.background_preemptions += 1
                    state.condition.notify_all()
                    while state.background_active:
                        await state.condition.wait()
                    state.foreground_active += 1
                finally:
                    state.foreground_waiting -= 1
                    state.condition.notify_all()
            return _AdmissionLease(priority="foreground", state=state)

        async with state.condition:
            state.background_requests += 1
            state.background_waiting += 1
            deferred = False
            try:
                while (
                    state.foreground_active
                    or state.foreground_waiting
                    or state.background_active
                ):
                    deferred = True
                    await state.condition.wait()
                state.background_active += 1
                lease = _AdmissionLease(priority="background", state=state)
                state.active_background.add(lease)
                if deferred:
                    state.background_deferred += 1
            finally:
                state.background_waiting -= 1
                state.condition.notify_all()
        return lease

    async def _release_admission(self, lease: _AdmissionLease) -> None:
        if lease.released:
            return
        state = lease.state

        # Admission counters are an authoritative scheduling barrier: a second
        # cancellation while waiting for the condition must not strand an active
        # background lease and deadlock every later foreground request.
        acquire_task = asyncio.create_task(state.condition.acquire())
        current = asyncio.current_task()
        baseline_cancellations = current.cancelling() if current is not None else 0
        cancellation_requested = False
        while True:
            try:
                await asyncio.shield(acquire_task)
                break
            except asyncio.CancelledError:
                if acquire_task.cancelled():
                    raise
                pending_cancellations = (
                    max(0, current.cancelling() - baseline_cancellations)
                    if current is not None
                    else 0
                )
                cancellation_requested = True
                if current is not None:
                    for _ in range(pending_cancellations):
                        current.uncancel()
                if acquire_task.done():
                    break
        acquire_task.result()
        try:
            if not lease.released:
                if lease.priority == "foreground":
                    state.foreground_active = max(0, state.foreground_active - 1)
                    state.foreground_completed += 1
                else:
                    state.active_background.discard(lease)
                    state.background_active = max(0, state.background_active - 1)
                    if not lease.preempted.is_set():
                        state.background_completed += 1
                lease.released = True
                state.condition.notify_all()
        finally:
            state.condition.release()
        if cancellation_requested:
            raise asyncio.CancelledError

    async def _await_or_preempt(
        self,
        awaitable: Awaitable[_T],
        lease: _AdmissionLease,
    ) -> _T:
        if lease.priority == "foreground":
            return await awaitable
        request = asyncio.ensure_future(awaitable)
        preemption = asyncio.create_task(lease.preempted.wait())
        try:
            done, _pending = await asyncio.wait(
                {request, preemption},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if preemption in done:
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request
                raise _BackgroundPreempted(BACKGROUND_PREEMPTED_ERROR)
            return request.result()
        finally:
            if not request.done():
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request
            preemption.cancel()
            with suppress(asyncio.CancelledError):
                await preemption

    async def _post_completion(
        self,
        body: dict[str, Any],
        lease: _AdmissionLease,
    ) -> dict[str, Any]:
        async def request() -> dict[str, Any]:
            timeout = httpx.Timeout(self.settings.llm_timeout_sec, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.settings.llm_base_url}/chat/completions",
                    json=body,
                )
                response.raise_for_status()
                return response.json()

        return await self._await_or_preempt(request(), lease)

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
        thinking_enabled: bool = True,
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
        if not thinking_enabled:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        priority = self._priority.get()
        while True:
            lease = await self._acquire_admission(priority)
            try:
                data = await self._post_completion(body, lease)
            except _BackgroundPreempted:
                # Generation is side-effect free. Discard the partial response,
                # yield to all foreground traffic, then retry the same prompt.
                continue
            except Exception as exc:  # noqa: BLE001
                return LLMResult(ok=False, content="", error=_exc_message(exc))
            finally:
                await self._release_admission(lease)
            break

        choices = data.get("choices") or []
        if not choices:
            return LLMResult(ok=False, content="", error="LLM response has no choices", raw=data)
        content = (choices[0].get("message") or {}).get("content") or ""
        content = content.strip()
        if detect_repeated_token_degeneration(content):
            return LLMResult(
                ok=False,
                content="",
                error=(
                    "Profile health probe failed: repeated-token/cyclic degeneration "
                    f"on profile {self.settings.profile.name}. Profile is unhealthy "
                    "and must not be reported as ready."
                ),
                raw=data,
            )
        return LLMResult(ok=True, content=content, raw=data)

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool = True,
        include_usage: bool = False,
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
        if not thinking_enabled:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        priority = self._priority.get()
        while True:
            lease = await self._acquire_admission(priority)
            buffered: list[LLMStreamChunk] = []
            retry = False
            error: str | None = None
            try:
                async for chunk in self._stream_completion(body, lease):
                    if priority == "background":
                        buffered.append(chunk)
                    else:
                        yield chunk
            except _BackgroundPreempted:
                # Background streams are buffered, so retrying cannot duplicate
                # partial output in a nested mission consumer.
                retry = True
            except Exception as exc:  # noqa: BLE001
                error = _exc_message(exc)
            finally:
                await self._release_admission(lease)
            if retry:
                continue
            if error is not None:
                yield LLMStreamChunk(kind="error", error=error)
                return
            # Release the background lease before exposing buffered output. A
            # slow nested consumer must never hold up newly arrived foreground
            # traffic after model generation has already finished.
            if priority == "background":
                for chunk in buffered:
                    yield chunk
            return

    async def _stream_completion(
        self,
        body: dict[str, Any],
        lease: _AdmissionLease,
    ) -> AsyncIterator[LLMStreamChunk]:
        timeout = httpx.Timeout(self.settings.llm_timeout_sec, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            stream = client.stream(
                "POST",
                f"{self.settings.llm_base_url}/chat/completions",
                json=body,
            )
            response = await self._await_or_preempt(stream.__aenter__(), lease)
            try:
                response.raise_for_status()
                lines = response.aiter_lines().__aiter__()
                finish_emitted = False
                while True:
                    try:
                        line = await self._await_or_preempt(lines.__anext__(), lease)
                    except StopAsyncIteration:
                        return
                    if _is_stream_done_marker(line):
                        if not finish_emitted:
                            yield LLMStreamChunk(kind="done")
                        return
                    chunk = _stream_chunk_from_line(line)
                    if chunk is None:
                        continue
                    if chunk.kind == "done":
                        finish_emitted = True
                    yield chunk
            finally:
                await stream.__aexit__(None, None, None)

    async def benchmark_inference(
        self,
        *,
        runs: int = 3,
        max_tokens: int = 64,
        timeout_sec: float = 30.0,
    ) -> dict[str, Any]:
        """Measure isolated streaming TTFT and decode rate with bounded work."""

        bounded_runs = max(1, min(3, int(runs)))
        bounded_tokens = max(8, min(64, int(max_tokens)))
        bounded_timeout = max(
            5.0,
            min(60.0, float(timeout_sec), float(self.settings.llm_timeout_sec)),
        )
        samples: list[dict[str, Any]] = []
        for index in range(1, bounded_runs + 1):
            try:
                sample = await asyncio.wait_for(
                    self._benchmark_inference_run(max_tokens=bounded_tokens),
                    timeout=bounded_timeout,
                )
            except TimeoutError:
                sample = {
                    "run": index,
                    "ok": False,
                    "error": f"inference benchmark timed out after {bounded_timeout:.1f}s",
                    "ttft_ms": None,
                    "total_ms": round(bounded_timeout * 1000, 2),
                    "completion_tokens": None,
                    "decode_tokens_per_sec": None,
                }
            else:
                sample["run"] = index
            samples.append(sample)

        successful = [item for item in samples if item.get("ok")]
        aggregate = {
            "ttft_ms_p50": _median_metric(successful, "ttft_ms"),
            "total_ms_p50": _median_metric(successful, "total_ms"),
            "completion_tokens_p50": _median_metric(successful, "completion_tokens"),
            "decode_tokens_per_sec_p50": _median_metric(
                successful,
                "decode_tokens_per_sec",
            ),
            "output_tokens_per_sec_p50": _median_metric(
                successful,
                "output_tokens_per_sec",
            ),
        }
        return {
            "ok": len(successful) == bounded_runs,
            "requested_runs": bounded_runs,
            "successful_runs": len(successful),
            "max_tokens": bounded_tokens,
            "timeout_sec_per_run": bounded_timeout,
            "usage_source": "openai-stream-usage",
            "runs": samples,
            "aggregate": aggregate,
        }

    async def _benchmark_inference_run(self, *, max_tokens: int) -> dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": "Return only the requested numbered list. Do not explain.",
            },
            {
                "role": "user",
                "content": (
                    "Write 24 numbered Russian color words, one word per line. "
                    "Repeat colors if needed."
                ),
            },
        ]
        started = time.perf_counter()
        first_token_at: float | None = None
        completion_tokens: int | None = None
        content_parts: list[str] = []
        error: str | None = None
        async for chunk in self.stream_complete(
            messages,
            temperature=0.0,
            max_tokens=max_tokens,
            thinking_enabled=False,
            include_usage=True,
        ):
            if chunk.kind == "delta" and chunk.content:
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                content_parts.append(chunk.content)
            usage = (chunk.raw or {}).get("usage")
            if isinstance(usage, dict):
                completion_tokens = _optional_int(usage.get("completion_tokens"))
            if chunk.kind == "error":
                error = chunk.error or "streaming inference failed"
                break
        finished = time.perf_counter()
        total_sec = max(0.0, finished - started)
        ttft_sec = (first_token_at - started) if first_token_at is not None else None
        decode_rate: float | None = None
        output_rate: float | None = None
        if completion_tokens is not None and completion_tokens > 0 and total_sec > 0:
            output_rate = completion_tokens / total_sec
            if ttft_sec is not None:
                decode_sec = max(0.001, total_sec - ttft_sec)
                decode_rate = max(0, completion_tokens - 1) / decode_sec
        usage_missing = completion_tokens is None
        if error is None and usage_missing:
            error = "stream completed without completion_tokens usage"
        return {
            "ok": error is None and first_token_at is not None,
            "error": error,
            "ttft_ms": round(ttft_sec * 1000, 2) if ttft_sec is not None else None,
            "total_ms": round(total_sec * 1000, 2),
            "completion_tokens": completion_tokens,
            "decode_tokens_per_sec": _rounded_rate(decode_rate),
            "output_tokens_per_sec": _rounded_rate(output_rate),
            "output_chars": len("".join(content_parts)),
        }


def _exc_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _rounded_rate(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 3)


def _median_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    values: list[float] = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, int | float) and math.isfinite(float(value)):
            values.append(float(value))
    return round(float(statistics.median(values)), 3) if values else None


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
        if isinstance(data.get("usage"), dict):
            return LLMStreamChunk(kind="usage", raw=data)
        return None
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content") or choice.get("text") or ""
    finish_reason = choice.get("finish_reason")
    finish = str(finish_reason) if finish_reason else None
    if content:
        # Some providers co-locate the final content delta with finish_reason.
        # Preserve both so length-auto-continue and truncation UX still work.
        return LLMStreamChunk(
            kind="delta",
            content=content,
            finish_reason=finish,
            raw=data,
        )
    if finish:
        return LLMStreamChunk(kind="done", finish_reason=finish, raw=data)
    return None


def _is_stream_done_marker(line: str) -> bool:
    value = line.strip()
    if value.startswith("data:"):
        value = value.removeprefix("data:").strip()
    return value == "[DONE]"
