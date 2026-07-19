"""Runtime notices: service/maintenance mode and model overload signals.

Persisted under the runtime state dir so admin toggles and supervisor probes share
one source of truth. Surfaced to the web UI as a banner and to Telegram as a
short reply when the operator or a TG principal would otherwise reach the agent.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_OVERLOAD_SAMPLES: list[tuple[float, float]] = []  # (monotonic_ts, latency_or_-1)
_OVERLOAD_FLAG = False
_OVERLOAD_SINCE: float | None = None
_OVERLOAD_REASON = ""

# Rolling window for latency samples used by overload detection.
_OVERLOAD_WINDOW_SEC = 180.0
_OVERLOAD_MIN_SAMPLES = 4
_OVERLOAD_LATENCY_SEC = 45.0
_OVERLOAD_ERROR_RATIO = 0.45
_OVERLOAD_CLEAR_SEC = 60.0


@dataclass(frozen=True)
class RuntimeNotice:
    kind: str  # service_mode | model_overload
    active: bool
    title: str
    message: str
    until: str | None = None
    since: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "active": self.active,
            "title": self.title,
            "message": self.message,
            "until": self.until,
            "since": self.since,
            "details": self.details or {},
        }


def _state_path(state_dir: Path) -> Path:
    return Path(state_dir) / "runtime_notices.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_state(state_dir: Path) -> dict[str, Any]:
    path = _state_path(state_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(state_dir: Path, payload: dict[str, Any]) -> None:
    path = _state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def set_service_mode(
    state_dir: Path,
    *,
    enabled: bool,
    message: str = "",
    until: str | None = None,
) -> dict[str, Any]:
    """Enable/disable maintenance mode. ``until`` is an ISO-8601 timestamp or None."""

    with _LOCK:
        state = _read_state(state_dir)
        service = {
            "enabled": bool(enabled),
            "message": (message or "").strip()[:500],
            "until": (until or "").strip() or None,
            "updated_at": _now_iso(),
        }
        if enabled and not service["message"]:
            service["message"] = "Ведутся технические работы. Сервис временно недоступен."
        state["service_mode"] = service
        _write_state(state_dir, state)
        return service


def get_service_mode(state_dir: Path) -> dict[str, Any]:
    with _LOCK:
        service = dict(_read_state(state_dir).get("service_mode") or {})
    enabled = bool(service.get("enabled"))
    until_raw = service.get("until")
    until_iso = str(until_raw).strip() if until_raw else None
    if enabled and until_iso:
        try:
            until_dt = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=UTC)
            if datetime.now(UTC) >= until_dt.astimezone(UTC):
                enabled = False
        except ValueError:
            pass
    return {
        "enabled": enabled,
        "message": str(service.get("message") or ""),
        "until": until_iso,
        "updated_at": service.get("updated_at"),
    }


def record_llm_sample(
    *,
    latency_sec: float | None = None,
    error: bool = False,
) -> None:
    """Feed one LLM completion attempt into the overload detector."""

    global _OVERLOAD_FLAG, _OVERLOAD_SINCE, _OVERLOAD_REASON
    now = time.monotonic()
    with _LOCK:
        sample = -1.0 if error else float(latency_sec or 0.0)
        _OVERLOAD_SAMPLES.append((now, sample))
        cutoff = now - _OVERLOAD_WINDOW_SEC
        while _OVERLOAD_SAMPLES and _OVERLOAD_SAMPLES[0][0] < cutoff:
            _OVERLOAD_SAMPLES.pop(0)

        if len(_OVERLOAD_SAMPLES) < _OVERLOAD_MIN_SAMPLES:
            return

        errors = sum(1 for _, v in _OVERLOAD_SAMPLES if v < 0)
        slow = sum(1 for _, v in _OVERLOAD_SAMPLES if v >= _OVERLOAD_LATENCY_SEC)
        total = len(_OVERLOAD_SAMPLES)
        error_ratio = errors / total
        slow_ratio = slow / total
        overloaded = error_ratio >= _OVERLOAD_ERROR_RATIO or slow_ratio >= 0.5

        if overloaded:
            if not _OVERLOAD_FLAG:
                _OVERLOAD_FLAG = True
                _OVERLOAD_SINCE = now
            if error_ratio >= _OVERLOAD_ERROR_RATIO:
                _OVERLOAD_REASON = (
                    f"Модель перегружена или недоступна "
                    f"({errors}/{total} ошибок за ~{int(_OVERLOAD_WINDOW_SEC)}с)."
                )
            else:
                _OVERLOAD_REASON = (
                    f"Модель отвечает слишком медленно "
                    f"({slow}/{total} запросов дольше {_OVERLOAD_LATENCY_SEC:.0f}с)."
                )
        elif _OVERLOAD_FLAG:
            since = _OVERLOAD_SINCE or now
            if (
                now - since >= _OVERLOAD_CLEAR_SEC
                and error_ratio < 0.2
                and slow_ratio < 0.25
            ):
                _OVERLOAD_FLAG = False
                _OVERLOAD_SINCE = None
                _OVERLOAD_REASON = ""


def get_overload_state() -> dict[str, Any]:
    with _LOCK:
        since_iso = None
        if _OVERLOAD_FLAG and _OVERLOAD_SINCE is not None:
            # Approximate wall clock from monotonic delta is not perfect; report flag only.
            since_iso = _now_iso()
        return {
            "active": bool(_OVERLOAD_FLAG),
            "message": _OVERLOAD_REASON
            or (
                "Модель перегружена. Запросы временно обрабатываются медленнее или отклоняются."
                if _OVERLOAD_FLAG
                else ""
            ),
            "since": since_iso if _OVERLOAD_FLAG else None,
        }


def force_overload(active: bool, *, reason: str = "") -> dict[str, Any]:
    """Test/admin helper to pin overload state."""

    global _OVERLOAD_FLAG, _OVERLOAD_SINCE, _OVERLOAD_REASON
    with _LOCK:
        _OVERLOAD_FLAG = bool(active)
        _OVERLOAD_SINCE = time.monotonic() if active else None
        _OVERLOAD_REASON = (reason or "").strip() if active else ""
        if active and not _OVERLOAD_REASON:
            _OVERLOAD_REASON = (
                "Модель перегружена. Запросы временно обрабатываются медленнее или отклоняются."
            )
        return get_overload_state()


def collect_notices(state_dir: Path) -> list[dict[str, Any]]:
    notices: list[RuntimeNotice] = []
    service = get_service_mode(state_dir)
    if service.get("enabled"):
        until = service.get("until")
        until_text = f" Ожидаемое время окончания: {until}." if until else ""
        message = str(service.get("message") or "Ведутся технические работы.") + until_text
        notices.append(
            RuntimeNotice(
                kind="service_mode",
                active=True,
                title="Технические работы",
                message=message.strip(),
                until=str(until) if until else None,
            )
        )
    overload = get_overload_state()
    if overload.get("active"):
        notices.append(
            RuntimeNotice(
                kind="model_overload",
                active=True,
                title="Перегрузка модели",
                message=str(overload.get("message") or ""),
                since=overload.get("since"),
            )
        )
    return [item.as_dict() for item in notices]


def blocking_notice(state_dir: Path) -> dict[str, Any] | None:
    """Return the highest-priority notice that should short-circuit a chat turn."""

    notices = collect_notices(state_dir)
    for item in notices:
        if item.get("kind") == "service_mode" and item.get("active"):
            return item
    for item in notices:
        if item.get("kind") == "model_overload" and item.get("active"):
            # Overload is advisory in the UI; for chat we still answer with a short
            # notice so TG/web users understand the delay without hanging forever.
            return item
    return None


def user_facing_reply(notice: dict[str, Any]) -> str:
    title = str(notice.get("title") or "Сервис").strip()
    message = str(notice.get("message") or "").strip()
    if notice.get("kind") == "service_mode":
        return f"🛠 {title}\n\n{message}".strip()
    if notice.get("kind") == "model_overload":
        return f"⏳ {title}\n\n{message}".strip()
    return message or title
