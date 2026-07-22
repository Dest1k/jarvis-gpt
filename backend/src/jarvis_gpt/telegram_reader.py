"""Credential-isolated adapter for an already authenticated Telegram reader CLI.

Jarvis never accepts Telegram credentials or session strings.  An operator may point
``JARVIS_TELEGRAM_READER_COMMAND_JSON`` at an absolute executable command which owns
its authenticated session and implements the small JSON request/response contract
below.  The child receives only a minimal OS environment, not Jarvis tokens/secrets.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .telegram_sources import (
    TelegramAuthorizedReader,
    TelegramReaderBatch,
    TelegramReaderCapability,
    TelegramReaderMedia,
    TelegramReaderPost,
    TelegramReaderSource,
)

_COMMAND_ENV = "JARVIS_TELEGRAM_READER_COMMAND_JSON"
_TIMEOUT_ENV = "JARVIS_TELEGRAM_READER_TIMEOUT_SEC"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_POSTS = 500
_SAFE_CHILD_ENV = frozenset(
    {
        "APPDATA",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


class TelegramReaderCommandError(RuntimeError):
    """The external reader failed without exposing its stdout/stderr or secrets."""


def _aware_datetime(value: Any, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise TelegramReaderCommandError(f"reader returned invalid {field}") from exc
    if parsed.tzinfo is None:
        raise TelegramReaderCommandError(f"reader returned timezone-naive {field}")
    return parsed


def _string_set(value: Any, *, allowed: frozenset[str]) -> frozenset[str]:
    if not isinstance(value, list | tuple | set):
        return frozenset()
    return frozenset(
        item
        for item in (str(raw or "").strip().casefold() for raw in value)
        if item in allowed
    )


class SubprocessTelegramAuthorizedReader(TelegramAuthorizedReader):
    """JSON-over-stdio bridge to an existing authenticated CLI/session runtime."""

    def __init__(self, command: tuple[str, ...], *, timeout_sec: float = 45.0) -> None:
        if not command:
            raise ValueError("Telegram reader command is empty")
        executable = Path(command[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("Telegram reader executable must be an existing absolute file")
        self._command = tuple(str(item) for item in command)
        self._timeout_sec = max(2.0, min(float(timeout_sec), 300.0))

    @staticmethod
    def _child_environment() -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _SAFE_CHILD_ENV and isinstance(value, str)
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    def _invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = json.dumps(
            {"protocol": "jarvis.telegram-reader.v1", "operation": operation, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            # Do not buffer arbitrary provider output in the Jarvis process.  The child
            # still inherits the host working directory for command compatibility,
            # but only the explicit minimal environment below crosses the boundary.
            with tempfile.TemporaryFile() as stdout:
                completed = subprocess.run(  # noqa: S603 - operator-owned executable
                    self._command,
                    input=request,
                    stdout=stdout,
                    stderr=subprocess.DEVNULL,
                    env=self._child_environment(),
                    shell=False,
                    timeout=self._timeout_sec,
                    check=False,
                )
                response_size = stdout.tell()
                if completed.returncode != 0 or response_size > _MAX_RESPONSE_BYTES:
                    raise TelegramReaderCommandError("Telegram reader command failed")
                stdout.seek(0)
                raw_response = stdout.read(_MAX_RESPONSE_BYTES + 1)
        except TelegramReaderCommandError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise TelegramReaderCommandError("Telegram reader command unavailable") from exc
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramReaderCommandError("Telegram reader returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise TelegramReaderCommandError("Telegram reader returned an invalid object")
        return response

    def capability(self) -> TelegramReaderCapability:
        response = self._invoke("capability", {})
        return TelegramReaderCapability(
            provider_name=str(response.get("provider_name") or ""),
            reader_identity_sha256=str(response.get("reader_identity_sha256") or ""),
            configured=response.get("configured") is True,
            authenticated=response.get("authenticated") is True,
            state=str(response.get("state") or "unknown")[:80],
            supports_history=response.get("supports_history") is True,
            supports_media=response.get("supports_media") is True,
            source_types=_string_set(
                response.get("source_types"),
                allowed=frozenset({"channel", "supergroup"}),
            ),
            access_scopes=_string_set(
                response.get("access_scopes"),
                allowed=frozenset({"public", "private"}),
            ),
        )

    def read_history(
        self,
        source: TelegramReaderSource,
        *,
        limit: int,
        before_message_id: int | None = None,
    ) -> TelegramReaderBatch:
        bounded = max(1, min(_MAX_POSTS, int(limit)))
        response = self._invoke(
            "read_history",
            {
                "source": {
                    "realm_id": source.realm_id,
                    "source_chat_id": source.source_chat_id,
                    "source_type": source.source_type,
                    "access_scope": source.access_scope,
                    "title": source.title[:255],
                    "username": source.username[:64],
                },
                "limit": bounded,
                "before_message_id": before_message_id,
            },
        )
        raw_posts = response.get("posts")
        if not isinstance(raw_posts, list) or len(raw_posts) > bounded:
            raise TelegramReaderCommandError("Telegram reader returned an invalid post page")
        posts: list[TelegramReaderPost] = []
        for raw_post in raw_posts:
            if not isinstance(raw_post, dict):
                raise TelegramReaderCommandError("Telegram reader returned an invalid post")
            message_id = raw_post.get("message_id")
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or message_id <= 0
            ):
                raise TelegramReaderCommandError("Telegram reader returned an invalid message id")
            raw_media = raw_post.get("media")
            if raw_media is None:
                raw_media = []
            if not isinstance(raw_media, list) or len(raw_media) > 50:
                raise TelegramReaderCommandError("Telegram reader returned invalid media")
            media: list[TelegramReaderMedia] = []
            for item in raw_media:
                if not isinstance(item, dict):
                    raise TelegramReaderCommandError("Telegram reader returned invalid media")
                raw_size = item.get("size")
                media.append(
                    TelegramReaderMedia(
                        kind=str(item.get("kind") or "")[:80],
                        stable_id=str(item.get("stable_id") or "")[:1000],
                        file_name=str(item.get("file_name") or "")[:512],
                        mime_type=str(item.get("mime_type") or "")[:255],
                        size=(
                            raw_size
                            if isinstance(raw_size, int)
                            and not isinstance(raw_size, bool)
                            and raw_size >= 0
                            else None
                        ),
                    )
                )
            edit_date = raw_post.get("edit_date")
            posts.append(
                TelegramReaderPost(
                    message_id=message_id,
                    text=str(raw_post.get("text") or "")[:20_000],
                    date=_aware_datetime(raw_post.get("date"), field="date"),
                    edit_date=(
                        _aware_datetime(edit_date, field="edit_date")
                        if edit_date not in {None, ""}
                        else None
                    ),
                    version_id=str(raw_post.get("version_id") or "")[:255],
                    permalink=str(raw_post.get("permalink") or "")[:1000],
                    media=tuple(media),
                )
            )
        next_before = response.get("next_before_message_id")
        if next_before is not None and (
            isinstance(next_before, bool)
            or not isinstance(next_before, int)
            or next_before <= 0
        ):
            raise TelegramReaderCommandError("Telegram reader returned an invalid cursor")
        complete = response.get("complete") is True
        if complete and next_before is not None:
            raise TelegramReaderCommandError(
                "Telegram reader returned a cursor for complete history"
            )
        return TelegramReaderBatch(
            posts=tuple(posts),
            complete=complete,
            next_before_message_id=next_before,
        )


def load_authorized_reader_from_environment() -> TelegramAuthorizedReader | None:
    """Load the optional external reader without accepting or logging credentials."""

    raw = os.environ.get(_COMMAND_ENV, "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(parsed, list)
        or not parsed
        or len(parsed) > 32
        or any(not isinstance(item, str) or not item or len(item) > 4096 for item in parsed)
    ):
        return None
    try:
        timeout_sec = float(os.environ.get(_TIMEOUT_ENV, "45"))
        return SubprocessTelegramAuthorizedReader(tuple(parsed), timeout_sec=timeout_sec)
    except (TypeError, ValueError):
        return None
