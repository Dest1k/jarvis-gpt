"""Telegram bot frontend for Jarvis.

A standalone long-lived process (NOT part of the FastAPI app) that relays private Telegram
messages to the backend HTTP API. The backend is the identity and authorization authority:
the bridge proves the immutable Telegram ``from.id`` using a separate shared secret and
receives a short-lived, user-scoped Jarvis session for every update.

Security is fail-closed. Group/sender-chat/bot updates and ambiguous private-chat identity
bindings are rejected before any backend call. ``TELEGRAM_ALLOWED_CHAT_IDS`` remains an
optional deployment restriction; when it is empty every real Telegram user is eligible for
automatic registration with the backend's least-privileged default preset.

Talks to Telegram over the raw Bot HTTP API with httpx long-polling (no aiogram / PTB
dependency — httpx is already a core dep) and to the backend as an ordinary API client.

Run it alongside the backend:  ``py -3.11 jarvis.py telegram-bridge``
Set in backend/.env.local:
  TELEGRAM_BOT_TOKEN=<@BotFather token>
  JARVIS_TELEGRAM_BRIDGE_SECRET=<long random secret shared with the backend>
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .config import default_home, load_local_env_file
from .notify import (
    answer_action_keyboard,
    operator_reply_keyboard,
    progress_stop_keyboard,
)
from .telegram_format import html_to_plain, render_telegram_html, split_telegram_html

log = logging.getLogger("jarvis.telegram")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_REDACTED = "[REDACTED]"


class _TelegramTokenRedactingFormatter(logging.Formatter):
    """Redact the bot token after the complete record, including traceback, is rendered."""

    def __init__(self, bot_token: str) -> None:
        super().__init__(_LOG_FORMAT)
        encoded = quote(bot_token, safe="")
        self._secrets = tuple(
            sorted(
                {bot_token, encoded, encoded.replace("%3A", "%3a")}.difference({""}),
                key=len,
                reverse=True,
            )
        )

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return text

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        # Formatter caches rendered exception text on the record. Sanitise that cache too,
        # so a later handler cannot reuse the unredacted traceback.
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return self._redact(rendered)


def _configure_logging(bot_token: str) -> None:
    """Configure bridge logging without ever exposing Telegram credential-bearing URLs."""

    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    formatter = _TelegramTokenRedactingFormatter(bot_token)
    for handler in root.handlers:
        handler.setFormatter(formatter)
    root.setLevel(logging.INFO)

    # httpx INFO records include the complete request URL. Telegram embeds the credential
    # in that URL, so those access-style records must never be emitted in the first place.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


TG_MSG_LIMIT = 4096
# Telegram bots can send documents up to 50 MB; guard a little under that.
TG_DOC_CAP = 45 * 1024 * 1024
TYPING_REFRESH_SEC = 4.0
_START_COMMANDS = {"/start"}
_RESET_COMMANDS = {"/new", "/reset", "/новый", "/сброс"}
_STOP_COMMANDS = {"/stop", "/cancel", "/отмена", "/стоп"}
_CAPTURE_COMMANDS = {"/note", "/inbox", "/capture", "/заметка", "/заметки"}
_HELP_COMMANDS = {"/help", "/помощь", "/commands", "/команды"}
_STATUS_COMMANDS = {"/status", "/статус"}
_BRIEFING_COMMANDS = {"/briefing", "/сводка", "/digest"}
_QUIET_COMMANDS = {"/quiet", "/тишина", "/quiet_hours"}
# After this many seconds of a still-running agent turn, ping the chat so a long
# research/document job does not look frozen on the phone.
_PROGRESS_STATUS_AFTER_SEC = 12.0
_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
)
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
# Telegram Bot API hard limit for sendPhoto (use document above this).
_TG_PHOTO_CAP = 9 * 1024 * 1024
# Reply-keyboard day console (exact button labels) + single-emoji gestures.
_CONSOLE_LABEL_TO_ACTION: dict[str, str] = {
    "📋 Сводка": "briefing",
    "📊 Статус": "status",
    "📥 Inbox": "inbox_list",
    "🛑 Стоп": "stop",
    "🆕 Новый чат": "new",
    "❓ Помощь": "help",
    "Сводка": "briefing",
    "Статус": "status",
    "Inbox": "inbox_list",
    "Стоп": "stop",
    "Новый чат": "new",
    "Помощь": "help",
    # One-tap emoji gestures (same actions).
    "📋": "briefing",
    "📊": "status",
    "📥": "inbox_list",
    "📌": "inbox_list",
    "🛑": "stop",
    "⏹": "stop",
    "🆕": "new",
    "❓": "help",
    "⏰": "quiet_help",
}
_USER_SESSION_HEADER = "X-Jarvis-User-Session"
_BRIDGE_SECRET_HEADER = "X-Jarvis-Bridge-Secret"
_BRIDGE_HOT_CACHE_SIZE = 4_096
_INBOX_MAX_ATTEMPTS = 3
_INBOX_TRANSIENT_MAX_ATTEMPTS = 288
_INBOX_TRANSIENT_MAX_AGE_SEC = 24 * 60 * 60
_INBOX_RETRY_DELAYS_SEC = (2.0, 10.0, 30.0, 60.0, 300.0)
_INBOX_TRANSIENT_ERROR = "transient_backend_failure"
_BACKEND_RETRY_CLASS_HEADER = "X-Jarvis-Retry-Class"
_BACKEND_LLM_OUTAGE_CLASS = "llm-outage"
_BACKEND_CHAT_REQUEST_IN_PROGRESS_CLASS = "chat-request-in-progress"
_OWNER_INVITE_UPDATE_KEY = "_jarvis_owner_invite_proof"
_OWNER_INVITE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_OWNER_INVITE_PROOF_RE = re.compile(r"^[0-9a-f]{64}$")

# Audio/video attachments are transcribed backend-side; the bridge only relays them and,
# for a spoken turn, mirrors the modality by replying with a synthesized voice note.
_AUDIO_MIME_PREFIXES = ("audio/", "video/")
_AUDIO_EXTENSIONS = frozenset(
    {
        ".ogg",
        ".oga",
        ".opus",
        ".mp3",
        ".wav",
        ".m4a",
        ".aac",
        ".flac",
        ".wma",
        ".mp4",
        ".webm",
        ".mov",
        ".mkv",
        ".m4v",
        ".avi",
    }
)


def _telegram_command(text: str) -> str:
    tokens = str(text or "").strip().split(maxsplit=1)
    if not tokens:
        return ""
    first_token = tokens[0].casefold()
    if not first_token.startswith("/"):
        return ""
    return first_token.split("@", 1)[0]


def _owner_invite_token(text: str) -> str | None:
    tokens = str(text or "").strip().split(maxsplit=1)
    if len(tokens) != 2 or _telegram_command(text) not in _START_COMMANDS:
        return None
    payload = tokens[1].strip()
    if not payload.startswith("owner_"):
        return None
    token = payload.removeprefix("owner_")
    return token if _OWNER_INVITE_TOKEN_RE.fullmatch(token) else None


def _owner_invite_proof_from_update(update: Mapping[str, object]) -> str | None:
    persisted = str(update.get(_OWNER_INVITE_UPDATE_KEY) or "").strip()
    if _OWNER_INVITE_PROOF_RE.fullmatch(persisted):
        return persisted
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or message.get("caption") or "")
    token = _owner_invite_token(text)
    if token is None:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sanitize_owner_invite_update(update: dict) -> dict:
    """Replace the raw one-time bearer before an update reaches durable storage."""

    proof = _owner_invite_proof_from_update(update)
    if proof is None or _OWNER_INVITE_UPDATE_KEY in update:
        return update
    message = update.get("message")
    if not isinstance(message, dict):
        return update
    field = "text" if message.get("text") else "caption"
    sanitized_message = {**message, field: "/start owner_[redacted]"}
    return {
        **update,
        _OWNER_INVITE_UPDATE_KEY: proof,
        "message": sanitized_message,
    }


def _retryable_backend_http_error(exc: httpx.HTTPError) -> bool:
    """Classify machine-marked failures for the longer durable retry budget."""

    if isinstance(exc, httpx.RequestError):
        return True
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    response = exc.response
    if (
        response.status_code == 503
        and response.headers.get(_BACKEND_RETRY_CLASS_HEADER, "").strip().casefold()
        == _BACKEND_LLM_OUTAGE_CLASS
    ):
        return True
    if (
        response.status_code == 409
        and response.headers.get(_BACKEND_RETRY_CLASS_HEADER, "").strip().casefold()
        == _BACKEND_CHAT_REQUEST_IN_PROGRESS_CLASS
    ):
        return True
    if response.status_code != 409:
        return False
    try:
        detail = str((response.json() or {}).get("detail") or "")
    except ValueError:
        return False
    return detail in {
        "Telegram update was already processed",
        "Telegram update processing lease changed during claim",
        "Telegram update processing lease was superseded",
    }


def _looks_like_audio(attachment: Mapping[str, object]) -> bool:
    mime = str(attachment.get("mime_type") or "").lower()
    if mime.startswith(_AUDIO_MIME_PREFIXES):
        return True
    name = str(attachment.get("name") or "").lower()
    return Path(name).suffix in _AUDIO_EXTENSIONS


def _looks_like_image(name: str, mime: str) -> bool:
    mime_l = str(mime or "").lower().split(";", 1)[0].strip()
    if mime_l in _IMAGE_MIME_TYPES or mime_l.startswith("image/"):
        return True
    return Path(str(name or "")).suffix.lower() in _IMAGE_EXTENSIONS


def _quick_capture_body(text: str, command: str) -> str | None:
    """Extract capture content from /note …, ``+ …`` or ``! …`` prefixes.

    Returns ``""`` when the command is present but empty (caller prompts for text),
    or ``None`` when the message is not a capture at all.
    """

    raw = str(text or "").strip()
    if command in _CAPTURE_COMMANDS:
        rest = raw.split(maxsplit=1)
        return rest[1].strip() if len(rest) > 1 else ""
    if raw.startswith("+ ") or raw.startswith("! "):
        return raw[2:].strip()
    if raw.startswith("+") or raw.startswith("!"):
        body = raw[1:].strip()
        return body if body else ""
    return None


def _console_action_for_text(text: str) -> str | None:
    """Map a reply-keyboard label or slash command to a console action name."""

    raw = str(text or "").strip()
    if not raw:
        return None
    if raw in _CONSOLE_LABEL_TO_ACTION:
        return _CONSOLE_LABEL_TO_ACTION[raw]
    command = _telegram_command(raw)
    if command in _HELP_COMMANDS:
        return "help"
    if command in _STATUS_COMMANDS:
        return "status"
    if command in _BRIEFING_COMMANDS:
        return "briefing"
    if command in _STOP_COMMANDS:
        return "stop"
    if command in _RESET_COMMANDS:
        return "new"
    if command in _START_COMMANDS:
        return "start"
    if command in _QUIET_COMMANDS:
        return "quiet"
    return None


def _quiet_command_spec(text: str) -> str | None:
    """Extract quiet-hours range from ``/quiet 23:00-08:00`` (or bare ``off``/``clear``).

    Returns the raw range string, empty string to clear, or None if not a quiet command.
    """

    raw = str(text or "").strip()
    command = _telegram_command(raw)
    if command not in _QUIET_COMMANDS:
        return None
    rest = raw.split(maxsplit=1)
    if len(rest) < 2:
        return ""  # show current / help
    value = rest[1].strip()
    if value.casefold() in {"off", "clear", "нет", "выкл", "0", "-"}:
        return "clear"
    return value


def _is_forwarded_message(message: Mapping[str, object] | dict) -> bool:
    """True when Telegram marks the update as a user-forwarded message."""

    if not isinstance(message, Mapping):
        return False
    if message.get("forward_date") is not None:
        return True
    if message.get("forward_from") or message.get("forward_from_chat"):
        return True
    if message.get("forward_sender_name") or message.get("forward_from_message_id"):
        return True
    origin = message.get("forward_origin")
    return isinstance(origin, Mapping) and bool(origin)


def _forward_source_label(message: Mapping[str, object] | dict) -> str:
    """Human-readable source for a forwarded message (best-effort)."""

    if not isinstance(message, Mapping):
        return "unknown"
    origin = message.get("forward_origin")
    if isinstance(origin, Mapping):
        otype = str(origin.get("type") or "")
        if otype == "user":
            sender = origin.get("sender_user")
            if isinstance(sender, Mapping):
                name = " ".join(
                    str(part)
                    for part in (sender.get("first_name"), sender.get("last_name"))
                    if part
                ).strip()
                username = str(sender.get("username") or "").strip()
                if name and username:
                    return f"{name} (@{username})"
                return name or (f"@{username}" if username else "user")
        if otype == "hidden_user":
            return str(origin.get("sender_user_name") or "hidden user")
        if otype in {"chat", "channel"}:
            chat = origin.get("sender_chat") or origin.get("chat")
            if isinstance(chat, Mapping):
                title = str(chat.get("title") or chat.get("username") or "").strip()
                if title:
                    return title
        if otype == "channel":
            chat = origin.get("chat")
            if isinstance(chat, Mapping):
                return str(chat.get("title") or chat.get("username") or "channel")
    fwd_from = message.get("forward_from")
    if isinstance(fwd_from, Mapping):
        name = " ".join(
            str(part)
            for part in (fwd_from.get("first_name"), fwd_from.get("last_name"))
            if part
        ).strip()
        username = str(fwd_from.get("username") or "").strip()
        if name and username:
            return f"{name} (@{username})"
        return name or (f"@{username}" if username else "user")
    fwd_chat = message.get("forward_from_chat")
    if isinstance(fwd_chat, Mapping):
        title = str(fwd_chat.get("title") or fwd_chat.get("username") or "").strip()
        if title:
            return title
    hidden = str(message.get("forward_sender_name") or "").strip()
    return hidden or "unknown"


def _help_text() -> str:
    return (
        "Джарвис на связи.\n"
        "Пульт внизу: Сводка · Статус · Inbox · Стоп · Новый чат · Помощь.\n"
        "Жесты: 📋 📊 📥 🛑 📌 · ⏰ — quiet hours.\n"
        "\n"
        "Команды:\n"
        "• /new — новый разговор\n"
        "• /stop — отменить текущий запрос\n"
        "• /status · /briefing — без LLM\n"
        "• /note … или `+ …` — быстрый захват в inbox\n"
        "• /quiet 23:00-08:00 — тихие часы (hold напоминаний)\n"
        "• /quiet off — выключить quiet hours\n"
        "• перешли сообщение боту — разберу как задачу\n"
        "• «каждое утро сводка» — ежедневный briefing"
    )


def _format_status_card(body: Mapping[str, object] | dict) -> str:
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    profile = settings.get("profile") if isinstance(settings.get("profile"), dict) else {}
    profile_name = str(
        profile.get("name") or settings.get("profile_name") or "?"
    )
    counters = body.get("counters") if isinstance(body.get("counters"), dict) else {}
    health = body.get("health") if isinstance(body.get("health"), list) else []
    bad = [
        item
        for item in health
        if isinstance(item, dict) and item.get("status") in {"warn", "error"}
    ]
    lines = [
        f"📊 Статус · profile `{profile_name}`",
        f"missions={counters.get('missions', '?')} "
        f"memories={counters.get('memories', '?')} "
        f"files={counters.get('files', '?')}",
    ]
    if bad:
        lines.append("Проблемы:")
        for item in bad[:5]:
            name = item.get("name") or item.get("component") or "?"
            lines.append(f"• {name}: {item.get('message') or item.get('status')}")
    else:
        lines.append("Health: ok")
    notices = body.get("notices") if isinstance(body.get("notices"), list) else []
    for notice in notices[:2]:
        if isinstance(notice, dict) and notice.get("message"):
            lines.append(f"notice: {notice.get('message')}")
    return "\n".join(lines)


def _format_briefing_card(body: Mapping[str, object] | dict) -> str:
    headline = str(body.get("headline") or "Сводка").strip()
    lines = [f"📋 {headline}"]
    focus = [str(x).strip() for x in (body.get("focus") or []) if str(x).strip()]
    if focus:
        lines.append("Фокус:")
        lines.extend(f"• {item}" for item in focus[:5])
    risks = [str(x).strip() for x in (body.get("risks") or []) if str(x).strip()]
    if risks:
        lines.append("Риски:")
        lines.extend(f"• {item}" for item in risks[:4])
    suggestions = [
        str(x).strip() for x in (body.get("suggestions") or []) if str(x).strip()
    ]
    if suggestions:
        lines.append("Что сделать:")
        lines.extend(f"• {item}" for item in suggestions[:4])
    pending = body.get("pending_approvals")
    if pending:
        lines.append(f"Ожидают approval: {pending}")
    return "\n".join(lines)


def _build_forward_task_prompt(message: Mapping[str, object] | dict, text: str) -> str:
    """Turn a forwarded Telegram message into an agent task prompt."""

    source = _forward_source_label(message)
    body = " ".join(str(text or "").split()).strip()
    entities = message.get("entities") if isinstance(message, Mapping) else None
    urls: list[str] = []
    if isinstance(entities, list):
        for ent in entities:
            if not isinstance(ent, Mapping):
                continue
            if ent.get("type") == "url" and body:
                try:
                    offset = int(ent.get("offset") or 0)
                    length = int(ent.get("length") or 0)
                except (TypeError, ValueError):
                    continue
                urls.append(body[offset : offset + length])
            if ent.get("type") == "text_link":
                url = str(ent.get("url") or "").strip()
                if url:
                    urls.append(url)
    # Also harvest bare URLs from text.
    for match in re.finditer(r"https?://\S+", body):
        urls.append(match.group(0).rstrip(").,;]}>\""))
    uniq_urls = list(dict.fromkeys(u for u in urls if u))
    lines = [
        "Пересланное сообщение — обработай как задачу (forward-as-task).",
        f"Источник: {source}",
    ]
    if body:
        lines.append("Текст:")
        lines.append(body[:8000])
    else:
        lines.append("Текст: (без текста — смотри вложения, если есть).")
    if uniq_urls:
        lines.append("Ссылки:")
        lines.extend(f"- {url}" for url in uniq_urls[:8])
    lines.append(
        "Сделай полезное на русском, коротко и по делу:\n"
        "• ссылка/статья — суммируй и предложи действие;\n"
        "• поручение/мысль — выполни или сложи в inbox/reminder;\n"
        "• документ/фото — разбери вложение;\n"
        "• не спрашивай лишнего, если можно действовать."
    )
    return "\n".join(lines)


def _wav_to_ogg_opus(wav: bytes) -> bytes | None:
    """Transcode WAV bytes to OGG/Opus so Telegram shows an inline voice note.

    Returns None (caller falls back to sendAudio) when ffmpeg is absent or fails.
    """

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not wav:
        return None
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-f",
                "ogg",
                "pipe:1",
            ],
            input=wav,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    allowed_chat_ids: frozenset[int]
    backend_url: str
    # Separate from the general API token: possession authorizes binding an immutable
    # Telegram identity to a short-lived Jarvis session.
    bridge_secret: str = ""
    # Optional standalone assertions. Production identity is derived from Telegram
    # getMe and uses the canonical realm ``telegram:<immutable bot id>``.
    realm_id: str = ""
    bot_id: int = 0
    # Deprecated compatibility hint. Backend permissions remain authoritative.
    owner_chat_ids: frozenset[int] = frozenset()
    api_token: str = ""
    poll_timeout: int = 25
    request_timeout: float = 300.0  # agent turns (missions, web, vision) can be long
    # Fair, bounded intake: one user cannot serialize or fill the whole bot forever.
    max_concurrent_updates: int = 4
    max_pending_updates: int = 64
    max_pending_per_user: int = 2
    intake_rate_per_minute: int = 12
    # Admin-pre-provisioned Telegram chat ids (guest until first message), JSON file.
    pre_provisioned_path: Path | None = None
    max_files_out: int = 6
    # Voice-out mirrors the input modality: a spoken reply is sent ONLY when the incoming
    # message was itself voice/audio. A text message always gets a text-only reply.
    voice_replies: bool = True
    voice_reply_max_chars: int = 1500
    # The Telegram chat -> backend conversation binding must outlive the bridge process.
    # ``load_config`` always supplies a durable path; None is kept only for explicitly
    # constructed test/embedded configurations.
    conversation_store_path: Path | None = None
    legacy_conversation_store_path: Path | None = None
    # Realm-less databases predate multi-bot isolation. Importing one into a custom
    # realm requires an explicit destination mapping. A formerly configured named realm
    # additionally requires an explicit source mapping; it is never inferred from the bot.
    legacy_conversation_realm_id: str | None = None
    legacy_conversation_source_realm_id: str | None = None


def load_config(env: Mapping[str, str] | None = None) -> TelegramConfig:
    """Build the config, failing closed when identity-bridge credentials are missing."""

    env = os.environ if env is None else env
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set — create a bot with @BotFather and put the "
            "token in backend/.env.local. Refusing to start."
        )
    bridge_secret = (env.get("JARVIS_TELEGRAM_BRIDGE_SECRET") or "").strip()
    if len(bridge_secret) < 32:
        raise SystemExit(
            "JARVIS_TELEGRAM_BRIDGE_SECRET must contain at least 32 characters — generate "
            "a long random secret and "
            "put it in backend/.env.local. Refusing to trust Telegram identities without it."
        )
    api_token = (env.get("JARVIS_API_TOKEN") or "").strip()
    if bridge_secret in {token, api_token}:
        raise SystemExit(
            "JARVIS_TELEGRAM_BRIDGE_SECRET must be distinct from TELEGRAM_BOT_TOKEN and "
            "JARVIS_API_TOKEN."
        )
    try:
        ids = frozenset(
            int(part)
            for part in re.split(r"[,\s]+", (env.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip())
            if part
        )
        configured_owners = frozenset(
            int(part)
            for part in re.split(r"[,\s]+", (env.get("TELEGRAM_OWNER_CHAT_IDS") or "").strip())
            if part
        )
    except ValueError as exc:
        raise SystemExit("Telegram chat ID lists must contain integers only.") from exc

    backend_url = (env.get("JARVIS_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")
    parsed_backend = urlsplit(backend_url)
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    allow_insecure_backend = (
        env.get("JARVIS_TELEGRAM_ALLOW_INSECURE_BACKEND") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not parsed_backend.hostname or parsed_backend.scheme not in {"http", "https"}:
        raise SystemExit("JARVIS_BACKEND_URL must be an absolute HTTP(S) URL.")
    if (
        parsed_backend.scheme != "https"
        and parsed_backend.hostname.casefold() not in loopback_hosts
        and not allow_insecure_backend
    ):
        raise SystemExit(
            "JARVIS_BACKEND_URL must use HTTPS outside loopback; set "
            "JARVIS_TELEGRAM_ALLOW_INSECURE_BACKEND=1 only for an isolated trusted network."
        )
    if configured_owners and ids:
        unknown_owners = configured_owners - ids
        if unknown_owners:
            raise SystemExit(
                "Every TELEGRAM_OWNER_CHAT_IDS entry must also be present in "
                "TELEGRAM_ALLOWED_CHAT_IDS."
            )
    # Kept only for backward-compatible UI/file-delivery hints. The bridge never sends an
    # access mode to the backend and therefore cannot grant owner privileges.
    owner_ids = configured_owners
    voice_replies = (env.get("TELEGRAM_VOICE_REPLIES") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    try:
        voice_max = int((env.get("TELEGRAM_VOICE_REPLY_MAX_CHARS") or "").strip())
    except (TypeError, ValueError):
        voice_max = 1500

    def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int((env.get(name) or str(default)).strip())
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    realm_id = (env.get("JARVIS_TELEGRAM_REALM_ID") or "").strip()
    if realm_id and (realm_id == "default" or len(realm_id) > 120):
        raise SystemExit(
            "JARVIS_TELEGRAM_REALM_ID, when set as an assertion, must be canonical "
            "and must not exceed 120 characters."
        )
    raw_bot_id = (env.get("JARVIS_TELEGRAM_BOT_ID") or "").strip()
    bot_id = 0
    if raw_bot_id:
        try:
            bot_id = int(raw_bot_id)
        except ValueError as exc:
            raise SystemExit("JARVIS_TELEGRAM_BOT_ID must be a positive integer.") from exc
        if bot_id <= 0:
            raise SystemExit("JARVIS_TELEGRAM_BOT_ID must be a positive integer.")
    if bool(realm_id) != bool(bot_id):
        raise SystemExit(
            "JARVIS_TELEGRAM_REALM_ID and JARVIS_TELEGRAM_BOT_ID assertions "
            "must be set together."
        )
    if bot_id and realm_id != f"telegram:{bot_id}":
        raise SystemExit(
            "JARVIS_TELEGRAM_REALM_ID must equal telegram:<JARVIS_TELEGRAM_BOT_ID>."
        )
    legacy_realm_id = (env.get("JARVIS_TELEGRAM_LEGACY_REALM_ID") or "").strip()
    if legacy_realm_id and len(legacy_realm_id) > 120:
        raise SystemExit("JARVIS_TELEGRAM_LEGACY_REALM_ID must not exceed 120 characters.")
    legacy_source_realm_id = (
        env.get("JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID") or ""
    ).strip()
    if legacy_source_realm_id and len(legacy_source_realm_id) > 120:
        raise SystemExit(
            "JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID must not exceed 120 characters."
        )
    state_dir = default_home() / "data" / "jarvis-gpt" / "state"
    main_store = state_dir / "jarvis.sqlite3"
    configured_store = (env.get("TELEGRAM_CONVERSATION_STORE_PATH") or "").strip()
    legacy_store = state_dir / "telegram_bridge.sqlite3"
    if configured_store:
        configured_legacy_store = Path(configured_store).expanduser()
        if configured_legacy_store.resolve() != main_store.resolve():
            legacy_store = configured_legacy_store
    return TelegramConfig(
        bot_token=token,
        allowed_chat_ids=ids,
        backend_url=backend_url,
        bridge_secret=bridge_secret,
        realm_id=realm_id,
        bot_id=bot_id,
        owner_chat_ids=owner_ids,
        api_token=api_token,
        pre_provisioned_path=state_dir / "telegram_pre_provisioned.json",
        voice_replies=voice_replies,
        voice_reply_max_chars=voice_max,
        max_concurrent_updates=bounded_int(
            "JARVIS_TELEGRAM_MAX_CONCURRENT_UPDATES", 4, 1, 32
        ),
        max_pending_updates=bounded_int(
            "JARVIS_TELEGRAM_MAX_PENDING_UPDATES", 64, 1, 10_000
        ),
        max_pending_per_user=bounded_int(
            "JARVIS_TELEGRAM_MAX_PENDING_PER_USER", 2, 1, 20
        ),
        intake_rate_per_minute=bounded_int(
            "JARVIS_TELEGRAM_BRIDGE_RATE_LIMIT_PER_MINUTE", 12, 1, 10_000
        ),
        # New bindings always live in the primary database and are therefore covered by
        # Jarvis backup/restore. The old override remains a migration source only.
        conversation_store_path=main_store,
        legacy_conversation_store_path=legacy_store,
        legacy_conversation_realm_id=legacy_realm_id or None,
        legacy_conversation_source_realm_id=legacy_source_realm_id or None,
    )


def _chunks(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Split a reply into <=limit-char pieces, preferring line boundaries."""

    out: list[str] = []
    buffer = ""
    for line in (text or "").splitlines(keepends=True):
        while len(line) > limit:
            if buffer:
                out.append(buffer)
                buffer = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(buffer) + len(line) > limit:
            out.append(buffer)
            buffer = line
        else:
            buffer += line
    if buffer:
        out.append(buffer)
    return out or [" "]


class TelegramConversationIsolationError(RuntimeError):
    """A backend conversation id is already bound to another Telegram principal."""


class TelegramConversationMigrationError(RuntimeError):
    """Legacy bindings could not be migrated without risking history loss or mixing."""


@dataclass(frozen=True)
class TelegramUserSession:
    """Short-lived backend session established from one authenticated Telegram update."""

    token: str
    user_id: str
    preset_key: str
    owner_invite_claimed: bool = False


class TelegramConversationStore:
    """Durable one-to-one Telegram principal -> backend conversation binding.

    The bridge allocates and commits a conversation id *before* the first backend turn.
    This closes the crash window that existed when the id was learned only from the
    response and lived in a process-local dict. SQLite transactions plus FULL synchronous
    mode make the mapping survive process restarts and sudden power loss.
    """

    def __init__(
        self,
        path: Path,
        *,
        realm_id: str = "default",
        legacy_path: Path | None = None,
        legacy_realm_id: str | None = None,
        legacy_source_realm_id: str | None = None,
    ) -> None:
        self.path = path
        self.realm_id = str(realm_id).strip() or "default"
        if len(self.realm_id) > 120:
            raise ValueError("Telegram realm_id must not exceed 120 characters")
        normalized_legacy_realm = (
            str(legacy_realm_id).strip() if legacy_realm_id is not None else ""
        )
        if len(normalized_legacy_realm) > 120:
            raise ValueError("Telegram legacy_realm_id must not exceed 120 characters")
        self.legacy_realm_id = normalized_legacy_realm or (
            "default" if self.realm_id == "default" else None
        )
        normalized_legacy_source_realm = (
            str(legacy_source_realm_id).strip()
            if legacy_source_realm_id is not None
            else ""
        )
        if len(normalized_legacy_source_realm) > 120:
            raise ValueError(
                "Telegram legacy_source_realm_id must not exceed 120 characters"
            )
        if normalized_legacy_source_realm == self.realm_id:
            raise ValueError(
                "Telegram legacy_source_realm_id must differ from the destination realm"
            )
        self.legacy_source_realm_id = normalized_legacy_source_realm or None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        external_migration = self._read_legacy_store(legacy_path)
        try:
            with self._connect(configure_journal=False) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._initialize_primary_store(conn, external_migration)
        except TelegramConversationMigrationError:
            raise
        except sqlite3.Error as exc:
            raise TelegramConversationMigrationError(
                "could not initialize Telegram conversation bindings safely"
            ) from exc
        # Switch journal mode only after every validation and migration transaction
        # succeeded. A rejected migration leaves both schema and journal mode unchanged.
        with self._connect():
            pass

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        if table not in {
            "telegram_conversations",
            "telegram_conversations_legacy_v1",
            "telegram_update_inbox",
            "telegram_store_migrations",
        }:
            raise ValueError("Unsupported Telegram binding table")
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT type FROM sqlite_schema WHERE name = ?",
            (table,),
        ).fetchone()
        if row is None:
            return False
        if row[0] != "table":
            raise TelegramConversationMigrationError(
                f"Telegram schema object {table!r} is not a table"
            )
        return True

    @classmethod
    def _read_binding_rows(
        cls,
        conn: sqlite3.Connection,
        *,
        table: str,
        source: str,
        realm_id: str | None,
    ) -> list[tuple[int, str, str, str, str | None]]:
        columns = cls._table_columns(conn, table)
        if not {"chat_id", "conversation_id"}.issubset(columns):
            raise TelegramConversationMigrationError(
                f"{source} Telegram database has no compatible binding table"
            )
        if "realm_id" in columns and realm_id is None:
            raise TelegramConversationMigrationError(
                f"{source} Telegram database requires an explicit realm"
            )
        access_mode_sql = (
            "access_mode" if "access_mode" in columns else "'owner' AS access_mode"
        )
        updated_at_sql = (
            "updated_at"
            if "updated_at" in columns
            else "CURRENT_TIMESTAMP AS updated_at"
        )
        user_id_sql = "user_id" if "user_id" in columns else "NULL AS user_id"
        where_sql = " WHERE realm_id = ?" if "realm_id" in columns else ""
        parameters = (realm_id,) if where_sql else ()
        raw_rows = conn.execute(
            f"""
            SELECT chat_id, conversation_id, {access_mode_sql}, {updated_at_sql},
                   {user_id_sql}
            FROM {table}{where_sql}
            ORDER BY chat_id
            """,  # noqa: S608 - table/fragments are fixed internal identifiers
            parameters,
        ).fetchall()
        migration_time = str(conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])
        normalized: list[tuple[int, str, str, str, str | None]] = []
        seen_chats: dict[int, tuple[str, str, str | None]] = {}
        seen_conversations: dict[str, int] = {}
        for raw_chat, raw_conversation, raw_mode, raw_updated, raw_user in raw_rows:
            if isinstance(raw_chat, bool) or not isinstance(raw_chat, int | str):
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an invalid chat_id"
                )
            chat_text = str(raw_chat).strip()
            if not re.fullmatch(r"-?[0-9]+", chat_text):
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an invalid chat_id"
                )
            try:
                chat_id = int(chat_text)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an invalid chat_id"
                ) from exc
            if chat_id <= 0 or chat_id > 2**63 - 1:
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an out-of-range chat_id"
                )
            conversation_id = str(raw_conversation or "").strip()
            if not conversation_id or len(conversation_id) > 512:
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an invalid conversation_id"
                )
            access_mode = str(raw_mode or "")
            if access_mode not in {"owner", "guest"}:
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an invalid access_mode"
                )
            updated_at = str(raw_updated or "").strip() or migration_time
            if len(updated_at) > 128:
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an invalid updated_at"
                )
            user_id = str(raw_user).strip() if raw_user is not None else None
            if user_id == "":
                user_id = None
            if user_id is not None and len(user_id) > 160:
                raise TelegramConversationMigrationError(
                    f"{source} Telegram binding contains an invalid user_id"
                )
            previous = seen_chats.get(chat_id)
            identity = (conversation_id, access_mode, user_id)
            if previous is not None:
                if previous != identity:
                    raise TelegramConversationMigrationError(
                        f"{source} Telegram store has conflicting rows for one chat_id"
                    )
                continue
            previous_chat = seen_conversations.get(conversation_id)
            if previous_chat is not None and previous_chat != chat_id:
                raise TelegramConversationMigrationError(
                    f"{source} Telegram store binds one conversation_id to multiple chats"
                )
            seen_chats[chat_id] = identity
            seen_conversations[conversation_id] = chat_id
            normalized.append(
                (chat_id, conversation_id, access_mode, updated_at, user_id)
            )
        return normalized

    def _read_legacy_store(
        self, legacy_path: Path | None
    ) -> tuple[str, list[tuple[int, str, str, str, str | None]]] | None:
        if legacy_path is None or not legacy_path.exists():
            return None
        resolved = legacy_path.resolve()
        if resolved == self.path.resolve():
            return None
        path_key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        try:
            legacy_uri = f"{resolved.as_uri()}?mode=ro"
            with sqlite3.connect(legacy_uri, uri=True, timeout=5.0) as legacy:
                # Pin all schema and row reads to one SQLite snapshot. A content hash in
                # the migration key makes later legacy writes eligible on the next start
                # instead of being hidden forever behind a path-only marker.
                legacy.execute("BEGIN")
                columns = self._table_columns(legacy, "telegram_conversations")
                if not columns:
                    raise TelegramConversationMigrationError(
                        "legacy Telegram database has no compatible binding table"
                    )
                realm_aware = "realm_id" in columns
                if not realm_aware and self.legacy_source_realm_id is not None:
                    raise TelegramConversationMigrationError(
                        "JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID cannot describe a "
                        "realm-less Telegram database"
                    )
                if not realm_aware and self.legacy_realm_id != self.realm_id:
                    raise TelegramConversationMigrationError(
                        "realm-less Telegram history requires an explicit matching "
                        "JARVIS_TELEGRAM_LEGACY_REALM_ID"
                    )
                source_realm: str | None = None
                if realm_aware:
                    source_realms = {
                        str(row[0])
                        for row in legacy.execute(
                            "SELECT DISTINCT realm_id FROM telegram_conversations"
                        ).fetchall()
                    }
                    source_realm = self.realm_id
                    if self.legacy_source_realm_id is not None:
                        if self.legacy_realm_id != self.realm_id:
                            raise TelegramConversationMigrationError(
                                "named external Telegram history requires an explicit "
                                "matching JARVIS_TELEGRAM_LEGACY_REALM_ID"
                            )
                        if self.legacy_source_realm_id not in source_realms:
                            raise TelegramConversationMigrationError(
                                "configured legacy source realm contains no external "
                                "Telegram history"
                            )
                        if self.realm_id in source_realms:
                            raise TelegramConversationMigrationError(
                                "external legacy source and canonical Telegram realms "
                                "both contain state"
                            )
                        source_realm = self.legacy_source_realm_id
                    elif "default" in source_realms and self.realm_id not in source_realms:
                        if self.legacy_realm_id != self.realm_id:
                            raise TelegramConversationMigrationError(
                                "default external Telegram history requires an explicit "
                                "matching JARVIS_TELEGRAM_LEGACY_REALM_ID"
                            )
                        source_realm = "default"
                    elif (
                        self.realm_id != "default"
                        and "default" in source_realms
                        and self.realm_id in source_realms
                        and self.legacy_realm_id == self.realm_id
                    ):
                        raise TelegramConversationMigrationError(
                            "external default and canonical Telegram realms both contain state"
                        )
                rows = self._read_binding_rows(
                    legacy,
                    table="telegram_conversations",
                    source="legacy",
                    realm_id=source_realm,
                )
                snapshot_sha256 = hashlib.sha256(
                    json.dumps(
                        rows,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                source_scope = (
                    f"realm:{source_realm}->realm:{self.realm_id}"
                    if realm_aware
                    else "realm-less"
                )
                source_key = (
                    f"legacy-file:{path_key}:{source_scope}:snapshot:{snapshot_sha256}"
                )
                return source_key, rows
        except TelegramConversationMigrationError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TelegramConversationMigrationError(
                "could not read legacy Telegram conversation bindings"
            ) from exc

    @staticmethod
    def _create_primary_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_conversations (
                realm_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL CHECK(access_mode IN ('owner', 'guest')),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                PRIMARY KEY(realm_id, chat_id),
                UNIQUE(realm_id, conversation_id)
            )
            """
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(telegram_conversations)")
        }
        if "user_id" not in columns:
            conn.execute("ALTER TABLE telegram_conversations ADD COLUMN user_id TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_conversations_realm_chat
            ON telegram_conversations(realm_id, chat_id)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_conversations_realm_conversation
            ON telegram_conversations(realm_id, conversation_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_update_inbox (
                realm_id TEXT NOT NULL,
                update_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('pending','processing','completed','rejected','failed')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT,
                lease_expires_at REAL,
                last_error TEXT,
                received_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(realm_id, update_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_telegram_update_inbox_claim
            ON telegram_update_inbox(realm_id, status, update_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_store_migrations (
                source_key TEXT PRIMARY KEY,
                realm_id TEXT NOT NULL,
                migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    @staticmethod
    def _primary_key_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple(
            str(row[1])
            for row in sorted((row for row in rows if int(row[5]) > 0), key=lambda row: row[5])
        )

    @staticmethod
    def _unique_indexes(
        conn: sqlite3.Connection, table: str
    ) -> set[tuple[str, ...]]:
        indexes: set[tuple[str, ...]] = set()
        for index_name, is_unique in conn.execute(
            'SELECT name, "unique" FROM pragma_index_list(?)',
            (table,),
        ).fetchall():
            if not is_unique:
                continue
            columns = tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (str(index_name),),
                ).fetchall()
            )
            indexes.add(columns)
        return indexes

    @classmethod
    def _validate_primary_schema(cls, conn: sqlite3.Connection) -> None:
        required_conversation_columns = {
            "realm_id",
            "chat_id",
            "conversation_id",
            "access_mode",
            "updated_at",
            "user_id",
        }
        if not required_conversation_columns.issubset(
            cls._table_columns(conn, "telegram_conversations")
        ):
            raise TelegramConversationMigrationError(
                "main Telegram database has an incompatible conversation schema"
            )
        if cls._primary_key_columns(conn, "telegram_conversations") != (
            "realm_id",
            "chat_id",
        ):
            raise TelegramConversationMigrationError(
                "main Telegram conversation primary key is not realm-scoped"
            )
        unique_indexes = cls._unique_indexes(conn, "telegram_conversations")
        required_unique = {
            ("realm_id", "chat_id"),
            ("realm_id", "conversation_id"),
        }
        if not required_unique.issubset(unique_indexes):
            raise TelegramConversationMigrationError(
                "main Telegram conversation uniqueness is not realm-scoped"
            )
        for columns in unique_indexes:
            principal_columns = {"chat_id", "conversation_id"}.intersection(columns)
            if principal_columns and "realm_id" not in columns:
                raise TelegramConversationMigrationError(
                    "main Telegram database contains cross-realm unique constraints"
                )

        required_inbox_columns = {
            "realm_id",
            "update_id",
            "chat_id",
            "payload_json",
            "status",
            "attempt_count",
            "lease_token",
            "lease_expires_at",
            "last_error",
            "received_at",
            "updated_at",
        }
        if not required_inbox_columns.issubset(
            cls._table_columns(conn, "telegram_update_inbox")
        ) or cls._primary_key_columns(conn, "telegram_update_inbox") != (
            "realm_id",
            "update_id",
        ):
            raise TelegramConversationMigrationError(
                "main Telegram inbox schema is not realm-scoped"
            )

        if not {"source_key", "realm_id", "migrated_at"}.issubset(
            cls._table_columns(conn, "telegram_store_migrations")
        ) or cls._primary_key_columns(conn, "telegram_store_migrations") != (
            "source_key",
        ):
            raise TelegramConversationMigrationError(
                "main Telegram migration ledger has an incompatible schema"
            )

    @classmethod
    def _validate_all_realm_rows(cls, conn: sqlite3.Connection) -> None:
        realm_rows = conn.execute(
            "SELECT DISTINCT realm_id FROM telegram_conversations ORDER BY realm_id"
        ).fetchall()
        for (raw_realm_id,) in realm_rows:
            realm_id = str(raw_realm_id or "").strip()
            if not realm_id or len(realm_id) > 120:
                raise TelegramConversationMigrationError(
                    "main Telegram database contains an invalid realm_id"
                )
            cls._read_binding_rows(
                conn,
                table="telegram_conversations",
                source="main",
                realm_id=realm_id,
            )

    def _apply_migration(
        self,
        conn: sqlite3.Connection,
        *,
        source_key: str,
        rows: list[tuple[int, str, str, str, str | None]],
    ) -> int:
        claimed = conn.execute(
            "SELECT realm_id FROM telegram_store_migrations WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if claimed is not None:
            if str(claimed[0]) != self.realm_id:
                raise TelegramConversationMigrationError(
                    "legacy Telegram snapshot was already claimed by another bot realm"
                )
            return 0
        current_rows = self._read_binding_rows(
            conn,
            table="telegram_conversations",
            source="main",
            realm_id=self.realm_id,
        )
        main_by_chat = {row[0]: row for row in current_rows}
        main_by_conversation = {row[1]: row for row in current_rows}
        for row in rows:
            existing_conversation = main_by_conversation.get(row[1])
            if existing_conversation is not None:
                if existing_conversation[0] != row[0]:
                    raise TelegramConversationMigrationError(
                        "legacy and main Telegram stores bind one conversation_id "
                        "to different chats"
                    )
                if existing_conversation[2] != row[2]:
                    raise TelegramConversationMigrationError(
                        "legacy and main Telegram stores disagree on access_mode"
                    )
                if (
                    existing_conversation[4] is not None
                    and row[4] is not None
                    and existing_conversation[4] != row[4]
                ):
                    raise TelegramConversationMigrationError(
                        "legacy and main Telegram stores disagree on user ownership"
                    )
                if existing_conversation[4] is None and row[4] is not None:
                    conn.execute(
                        """
                        UPDATE telegram_conversations SET user_id = ?
                        WHERE realm_id = ? AND chat_id = ? AND user_id IS NULL
                        """,
                        (row[4], self.realm_id, row[0]),
                    )
        migrated = 0
        for chat_id, conversation_id, access_mode, updated_at, user_id in rows:
            if chat_id in main_by_chat:
                continue
            conn.execute(
                """
                INSERT INTO telegram_conversations(
                    realm_id, chat_id, conversation_id, access_mode, updated_at, user_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.realm_id,
                    chat_id,
                    conversation_id,
                    access_mode,
                    updated_at,
                    user_id,
                ),
            )
            migrated += 1
        conn.execute(
            """
            INSERT INTO telegram_store_migrations(source_key, realm_id)
            VALUES (?, ?)
            """,
            (source_key, self.realm_id),
        )
        if migrated:
            log.info(
                "Migrated %d Telegram conversation binding(s) into main database",
                migrated,
            )
        return migrated

    def _migrate_legacy_realm(
        self,
        conn: sqlite3.Connection,
        *,
        external_source_present: bool = False,
    ) -> int:
        """Atomically upgrade an explicitly identified former bot realm.

        Releases before canonical bot realms stored bridge bindings, durable inbox
        rows and IAM Telegram identities under ``default`` or an operator-configured
        name. Silently ignoring those rows loses history; silently claiming an arbitrary
        realm for the current token can cross bot tenants. The destination and every
        non-default source are therefore explicit, and mixed source/target state fails.
        """

        source_realm = self.legacy_source_realm_id or "default"
        if source_realm == self.realm_id:
            return 0
        if (
            self.legacy_source_realm_id is not None
            and self.legacy_realm_id != self.realm_id
        ):
            raise TelegramConversationMigrationError(
                "named Telegram realm history requires an explicit matching "
                "JARVIS_TELEGRAM_LEGACY_REALM_ID"
            )
        marker_key = f"realm-upgrade:{source_realm}:{self.realm_id}"
        scoped_tables = (
            ("telegram_conversations", ""),
            ("telegram_update_inbox", ""),
            ("telegram_store_migrations", ""),
            ("telegram_updates", ""),
            ("external_identities", " AND provider = 'telegram'"),
        )
        available: list[tuple[str, str]] = []
        source_total = 0
        canonical_total = 0
        for table, suffix in scoped_tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            columns = {
                str(row[1])
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            if "realm_id" not in columns:
                continue
            available.append((table, suffix))
            source_total += int(
                conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f"WHERE realm_id = ?{suffix}",  # noqa: S608 - fixed table/suffix
                    (source_realm,),
                ).fetchone()[0]
            )
            canonical_total += int(
                conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f"WHERE realm_id = ?{suffix}",  # noqa: S608 - fixed table/suffix
                    (self.realm_id,),
                ).fetchone()[0]
            )
        marker = conn.execute(
            "SELECT 1 FROM telegram_store_migrations "
            "WHERE source_key = ? AND realm_id = ?",
            (marker_key, self.realm_id),
        ).fetchone()
        if marker is not None:
            if source_total:
                raise TelegramConversationMigrationError(
                    "legacy Telegram realm contains new state after its migration marker"
                )
            return 0
        if source_total == 0:
            if (
                self.legacy_source_realm_id is not None
                and not external_source_present
            ):
                raise TelegramConversationMigrationError(
                    "configured legacy source realm contains no Telegram state"
                )
            return 0
        if self.legacy_realm_id != self.realm_id:
            raise TelegramConversationMigrationError(
                f"{source_realm} Telegram realm history requires an explicit matching "
                "JARVIS_TELEGRAM_LEGACY_REALM_ID"
            )
        if canonical_total:
            raise TelegramConversationMigrationError(
                "legacy source and canonical Telegram realms both contain state"
            )
        migrated = 0
        for table, suffix in available:
            cursor = conn.execute(
                f'UPDATE "{table}" SET realm_id = ? '
                f"WHERE realm_id = ?{suffix}",  # noqa: S608 - fixed table/suffix
                (self.realm_id, source_realm),
            )
            migrated += int(cursor.rowcount)
        conn.execute(
            """
            INSERT INTO telegram_store_migrations(source_key, realm_id)
            VALUES (?, ?)
            """,
            (marker_key, self.realm_id),
        )
        log.info(
            "Migrated %d Telegram realm-scoped row(s) from %s to %s",
            migrated,
            source_realm,
            self.realm_id,
        )
        return migrated

    def _initialize_primary_store(
        self,
        conn: sqlite3.Connection,
        external_migration: (
            tuple[str, list[tuple[int, str, str, str, str | None]]] | None
        ),
    ) -> None:
        if self._table_exists(conn, "telegram_conversations_legacy_v1"):
            raise TelegramConversationMigrationError(
                "an unfinished Telegram schema migration already exists"
            )
        columns = self._table_columns(conn, "telegram_conversations")
        inline_rows: list[tuple[int, str, str, str, str | None]] | None = None
        if columns and "realm_id" not in columns:
            if self.legacy_realm_id != self.realm_id:
                raise TelegramConversationMigrationError(
                    "realm-less Telegram history requires an explicit matching "
                    "JARVIS_TELEGRAM_LEGACY_REALM_ID"
                )
            if self._table_exists(conn, "telegram_store_migrations"):
                claimed_inline = conn.execute(
                    "SELECT 1 FROM telegram_store_migrations WHERE source_key = ?",
                    ("inline:telegram_conversations_legacy_v1",),
                ).fetchone()
                if claimed_inline is not None:
                    raise TelegramConversationMigrationError(
                        "realm-less Telegram history conflicts with its migration marker"
                    )
            inline_rows = self._read_binding_rows(
                conn,
                table="telegram_conversations",
                source="main legacy",
                realm_id=None,
            )
            conn.execute(
                "ALTER TABLE telegram_conversations "
                "RENAME TO telegram_conversations_legacy_v1"
            )
        elif columns:
            required = {
                "realm_id",
                "chat_id",
                "conversation_id",
                "access_mode",
                "updated_at",
            }
            if not required.issubset(columns):
                raise TelegramConversationMigrationError(
                    "main Telegram database has an incompatible realm-aware schema"
                )
            self._read_binding_rows(
                conn,
                table="telegram_conversations",
                source="main",
                realm_id=self.realm_id,
            )

        self._create_primary_schema(conn)
        self._validate_primary_schema(conn)
        self._validate_all_realm_rows(conn)
        self._migrate_legacy_realm(
            conn,
            external_source_present=external_migration is not None,
        )
        if inline_rows is not None:
            self._apply_migration(
                conn,
                source_key="inline:telegram_conversations_legacy_v1",
                rows=inline_rows,
            )
            # A realm-less source may be claimed by exactly one bot realm.
            conn.execute("DROP TABLE telegram_conversations_legacy_v1")
        if external_migration is not None:
            source_key, rows = external_migration
            self._apply_migration(conn, source_key=source_key, rows=rows)
        self._validate_all_realm_rows(conn)

    def _connect(self, *, configure_journal: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        if configure_journal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @staticmethod
    def _new_conversation_id() -> str:
        return f"tg_{uuid.uuid4().hex}"

    @staticmethod
    def _validate_principal(chat_id: int, access_mode: str, user_id: str) -> str:
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("Telegram chat_id must be an integer")
        if chat_id <= 0 or chat_id > 2**63 - 1:
            raise ValueError("Telegram chat_id is out of range")
        if access_mode not in {"owner", "guest"}:
            raise ValueError("Telegram access_mode must be owner or guest")
        normalized_user_id = str(user_id).strip()
        if not normalized_user_id or len(normalized_user_id) > 160:
            raise ValueError("Telegram user_id must contain 1 to 160 characters")
        return normalized_user_id

    def _raise_ownership_mismatch(self, chat_id: int) -> None:
        log.error(
            "Telegram conversation ownership mismatch realm=%s chat_id=%s",
            self.realm_id,
            chat_id,
        )
        raise TelegramConversationIsolationError(
            "Telegram chat binding belongs to another backend user"
        )

    def next_update_offset(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(update_id), -1) FROM telegram_update_inbox "
                "WHERE realm_id = ?",
                (self.realm_id,),
            ).fetchone()
        return int(row[0]) + 1

    def persist_updates(self, updates: list[tuple[int, int, dict]]) -> int:
        """Durably stage Telegram updates before advancing the remote polling offset."""

        if not updates:
            return 0
        now = time.time()
        inserted = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for update_id, chat_id, payload in updates:
                durable_payload = _sanitize_owner_invite_update(payload)
                cursor = conn.execute(
                    """
                    INSERT INTO telegram_update_inbox(
                        realm_id, update_id, chat_id, payload_json, status,
                        attempt_count, received_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                    ON CONFLICT(realm_id, update_id) DO NOTHING
                    """,
                    (
                        self.realm_id,
                        update_id,
                        chat_id,
                        json.dumps(
                            durable_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
                inserted += max(0, cursor.rowcount)
            conn.execute(
                """
                DELETE FROM telegram_update_inbox
                WHERE realm_id = ?
                  AND (
                      status IN ('completed', 'rejected')
                      OR (
                          status = 'failed'
                          AND COALESCE(last_error, '') <> ?
                          AND attempt_count >= ?
                      )
                  )
                  AND updated_at < ?
                """,
                (
                    self.realm_id,
                    _INBOX_TRANSIENT_ERROR,
                    _INBOX_MAX_ATTEMPTS,
                    now - 7 * 86_400,
                ),
            )
        return inserted

    def claim_pending_updates(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[tuple[dict, str]]:
        """Claim at most one ordered update per user, with a crash-recoverable lease."""

        bounded_limit = max(0, min(int(limit), 1_000))
        if bounded_limit == 0:
            return []
        now = time.time()
        leases: list[tuple[dict, str]] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT i.update_id, i.payload_json
                FROM telegram_update_inbox i
                WHERE i.realm_id = ?
                  AND (
                      i.status = 'pending'
                      OR (
                          i.status = 'failed'
                          AND (
                              (
                                  i.last_error = ?
                                  AND i.attempt_count < ?
                                  AND i.received_at >= ?
                              )
                              OR (
                                  COALESCE(i.last_error, '') <> ?
                                  AND i.attempt_count < ?
                              )
                          )
                          AND i.updated_at + CASE
                              WHEN i.attempt_count <= 1 THEN ?
                              WHEN i.attempt_count = 2 THEN ?
                              WHEN i.attempt_count <= 4 THEN ?
                              WHEN i.attempt_count <= 8 THEN ?
                              ELSE ?
                          END <= ?
                      )
                      OR (
                          i.status = 'processing'
                          AND i.lease_expires_at < ?
                          AND i.attempt_count < ?
                          AND i.received_at >= ?
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM telegram_update_inbox active
                      WHERE active.realm_id = i.realm_id
                        AND active.chat_id = i.chat_id
                        AND active.status = 'processing'
                        AND active.lease_expires_at >= ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM telegram_update_inbox earlier
                      WHERE earlier.realm_id = i.realm_id
                        AND earlier.chat_id = i.chat_id
                        AND earlier.update_id < i.update_id
                        AND (
                            earlier.status = 'pending'
                            OR (
                                earlier.status = 'processing'
                                AND earlier.lease_expires_at < ?
                                AND earlier.attempt_count < ?
                                AND earlier.received_at >= ?
                            )
                            OR (
                                earlier.status = 'failed'
                                AND (
                                    (
                                        earlier.last_error = ?
                                        AND earlier.attempt_count < ?
                                        AND earlier.received_at >= ?
                                    )
                                    OR (
                                        COALESCE(earlier.last_error, '') <> ?
                                        AND earlier.attempt_count < ?
                                    )
                                )
                            )
                        )
                  )
                ORDER BY i.update_id
                LIMIT ?
                """,
                (
                    self.realm_id,
                    _INBOX_TRANSIENT_ERROR,
                    _INBOX_TRANSIENT_MAX_ATTEMPTS,
                    now - _INBOX_TRANSIENT_MAX_AGE_SEC,
                    _INBOX_TRANSIENT_ERROR,
                    _INBOX_MAX_ATTEMPTS,
                    _INBOX_RETRY_DELAYS_SEC[0],
                    _INBOX_RETRY_DELAYS_SEC[1],
                    _INBOX_RETRY_DELAYS_SEC[2],
                    _INBOX_RETRY_DELAYS_SEC[3],
                    _INBOX_RETRY_DELAYS_SEC[4],
                    now,
                    now,
                    _INBOX_TRANSIENT_MAX_ATTEMPTS,
                    now - _INBOX_TRANSIENT_MAX_AGE_SEC,
                    now,
                    now,
                    _INBOX_TRANSIENT_MAX_ATTEMPTS,
                    now - _INBOX_TRANSIENT_MAX_AGE_SEC,
                    _INBOX_TRANSIENT_ERROR,
                    _INBOX_TRANSIENT_MAX_ATTEMPTS,
                    now - _INBOX_TRANSIENT_MAX_AGE_SEC,
                    _INBOX_TRANSIENT_ERROR,
                    _INBOX_MAX_ATTEMPTS,
                    bounded_limit,
                ),
            ).fetchall()
            for update_id, payload_json in rows:
                lease_token = uuid.uuid4().hex
                cursor = conn.execute(
                    """
                    UPDATE telegram_update_inbox
                    SET status = 'processing', attempt_count = attempt_count + 1,
                        lease_token = ?, lease_expires_at = ?, last_error = NULL,
                        updated_at = ?
                    WHERE realm_id = ? AND update_id = ?
                      AND (
                          status = 'pending'
                          OR (
                              status = 'failed'
                              AND (
                                  (
                                      last_error = ?
                                      AND attempt_count < ?
                                      AND received_at >= ?
                                  )
                                  OR (
                                      COALESCE(last_error, '') <> ?
                                      AND attempt_count < ?
                                  )
                              )
                              AND updated_at + CASE
                                  WHEN attempt_count <= 1 THEN ?
                                  WHEN attempt_count = 2 THEN ?
                                  WHEN attempt_count <= 4 THEN ?
                                  WHEN attempt_count <= 8 THEN ?
                                  ELSE ?
                              END <= ?
                          )
                          OR (
                              status = 'processing'
                              AND lease_expires_at < ?
                              AND attempt_count < ?
                              AND received_at >= ?
                          )
                      )
                    """,
                    (
                        lease_token,
                        now + max(60, int(lease_seconds)),
                        now,
                        self.realm_id,
                        update_id,
                        _INBOX_TRANSIENT_ERROR,
                        _INBOX_TRANSIENT_MAX_ATTEMPTS,
                        now - _INBOX_TRANSIENT_MAX_AGE_SEC,
                        _INBOX_TRANSIENT_ERROR,
                        _INBOX_MAX_ATTEMPTS,
                        _INBOX_RETRY_DELAYS_SEC[0],
                        _INBOX_RETRY_DELAYS_SEC[1],
                        _INBOX_RETRY_DELAYS_SEC[2],
                        _INBOX_RETRY_DELAYS_SEC[3],
                        _INBOX_RETRY_DELAYS_SEC[4],
                        now,
                        now,
                        _INBOX_TRANSIENT_MAX_ATTEMPTS,
                        now - _INBOX_TRANSIENT_MAX_AGE_SEC,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                try:
                    payload = json.loads(str(payload_json))
                except (TypeError, ValueError):
                    payload = None
                if not isinstance(payload, dict):
                    conn.execute(
                        """
                        UPDATE telegram_update_inbox
                        SET status = 'rejected', lease_token = NULL,
                            lease_expires_at = NULL, last_error = 'invalid_payload',
                            updated_at = ?
                        WHERE realm_id = ? AND update_id = ? AND lease_token = ?
                        """,
                        (now, self.realm_id, update_id, lease_token),
                    )
                    continue
                leases.append((payload, lease_token))
        return leases

    def finalize_update(
        self,
        update_id: int,
        lease_token: str,
        *,
        status: str,
        error: str | None = None,
    ) -> bool:
        if status not in {"completed", "rejected", "failed", "pending"}:
            raise ValueError("Invalid Telegram inbox final status")
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_update_inbox
                SET status = ?, lease_token = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE realm_id = ? AND update_id = ?
                  AND status = 'processing' AND lease_token = ?
                """,
                (
                    status,
                    (error or "")[:160] or None,
                    now,
                    self.realm_id,
                    update_id,
                    lease_token,
                ),
            )
        return cursor.rowcount == 1

    def load_all(self) -> dict[int, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id, conversation_id FROM telegram_conversations "
                "WHERE realm_id = ?",
                (self.realm_id,),
            ).fetchall()
        return {int(chat_id): str(conversation_id) for chat_id, conversation_id in rows}

    def get_or_create(
        self,
        chat_id: int,
        access_mode: str,
        *,
        user_id: str,
    ) -> str:
        user_id = self._validate_principal(chat_id, access_mode, user_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT conversation_id, access_mode, user_id
                FROM telegram_conversations
                WHERE realm_id = ? AND chat_id = ?
                """,
                (self.realm_id, chat_id),
            ).fetchone()
            if row is not None:
                stored_user_id = str(row[2]) if row[2] is not None else None
                if stored_user_id is not None and stored_user_id != user_id:
                    self._raise_ownership_mismatch(chat_id)
                # IAM presets control the next turn's capabilities, not conversation
                # identity. Preserve history across role changes; only /new and /reset
                # intentionally rotate a Telegram conversation.
                if stored_user_id is None or str(row[1]) != access_mode:
                    conn.execute(
                        """
                        UPDATE telegram_conversations
                        SET user_id = ?, access_mode = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE realm_id = ? AND chat_id = ?
                          AND (user_id IS NULL OR user_id = ?)
                        """,
                        (user_id, access_mode, self.realm_id, chat_id, user_id),
                    )
                return str(row[0])

            conversation_id = self._new_conversation_id()
            conn.execute(
                """
                INSERT INTO telegram_conversations(
                    realm_id, chat_id, conversation_id, access_mode, updated_at, user_id
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(realm_id, chat_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    access_mode = excluded.access_mode,
                    user_id = excluded.user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.realm_id, chat_id, conversation_id, access_mode, user_id),
            )
            return conversation_id

    def rotate(
        self,
        chat_id: int,
        access_mode: str,
        *,
        user_id: str,
    ) -> str:
        user_id = self._validate_principal(chat_id, access_mode, user_id)
        conversation_id = self._new_conversation_id()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT user_id FROM telegram_conversations
                WHERE realm_id = ? AND chat_id = ?
                """,
                (self.realm_id, chat_id),
            ).fetchone()
            if row is not None and row[0] is not None and str(row[0]) != user_id:
                self._raise_ownership_mismatch(chat_id)
            conn.execute(
                """
                INSERT INTO telegram_conversations(
                    realm_id, chat_id, conversation_id, access_mode, updated_at, user_id
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(realm_id, chat_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    access_mode = excluded.access_mode,
                    user_id = excluded.user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.realm_id, chat_id, conversation_id, access_mode, user_id),
            )
        return conversation_id

    def bind(
        self,
        chat_id: int,
        conversation_id: str,
        access_mode: str,
        *,
        user_id: str,
    ) -> None:
        """Accept a backend-normalized id without allowing cross-chat reuse."""

        user_id = self._validate_principal(chat_id, access_mode, user_id)
        conversation_id = str(conversation_id).strip()
        if not conversation_id or len(conversation_id) > 512:
            raise ValueError("Telegram conversation_id must contain 1 to 512 characters")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """
                SELECT user_id FROM telegram_conversations
                WHERE realm_id = ? AND chat_id = ?
                """,
                (self.realm_id, chat_id),
            ).fetchone()
            if (
                current is not None
                and current[0] is not None
                and str(current[0]) != user_id
            ):
                self._raise_ownership_mismatch(chat_id)
            owner = conn.execute(
                """
                SELECT chat_id FROM telegram_conversations
                WHERE realm_id = ? AND conversation_id = ? AND chat_id != ?
                """,
                (self.realm_id, conversation_id, chat_id),
            ).fetchone()
            if owner is not None:
                raise TelegramConversationIsolationError(
                    "backend conversation id is already bound to another Telegram chat"
                )
            conn.execute(
                """
                INSERT INTO telegram_conversations(
                    realm_id, chat_id, conversation_id, access_mode, updated_at, user_id
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(realm_id, chat_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    access_mode = excluded.access_mode,
                    user_id = excluded.user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.realm_id, chat_id, conversation_id, access_mode, user_id),
            )


class TelegramBridge:
    def __init__(
        self,
        cfg: TelegramConfig,
        *,
        tg_client: httpx.AsyncClient | None = None,
        api_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self._realm_id = cfg.realm_id
        self._bot_id = cfg.bot_id
        self._pre_provisioned_cache: frozenset[int] = frozenset()
        self._pre_provisioned_mtime: float | None = None
        self.tg = tg_client or httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{cfg.bot_token}",
            timeout=cfg.poll_timeout + 15,
            trust_env=False,
        )
        self._tg_file_base = f"https://api.telegram.org/file/bot{cfg.bot_token}"
        headers = {"Authorization": f"Bearer {cfg.api_token}"} if cfg.api_token else {}
        self.api = api_client or httpx.AsyncClient(
            base_url=cfg.backend_url,
            headers=headers,
            timeout=cfg.request_timeout,
            trust_env=False,
        )
        # Production must verify getMe before it can read or migrate bot-scoped history.
        # Tests and embedders establish the same identity explicitly through
        # ``_initialize_bot_identity`` before handling updates.
        self._conversation_store: TelegramConversationStore | None = None
        # These are bounded hot caches only; the durable store is queried lazily.
        self.conversations: OrderedDict[int, str] = OrderedDict()
        self._conversation_modes: OrderedDict[int, str] = OrderedDict()
        self._sessions: OrderedDict[int, TelegramUserSession] = OrderedDict()
        self._offset = 0
        self._update_slots = asyncio.Semaphore(max(1, cfg.max_concurrent_updates))
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._pending_per_chat: dict[int, int] = {}
        self._update_tasks: set[asyncio.Task[None]] = set()
        self._intake_windows: OrderedDict[int, tuple[float, int]] = OrderedDict()
        self._closing = False
        # Active agent-turn tasks per chat (for /stop). Not the queue wrapper — the
        # work inside the single-flight lock, so cancel can interrupt a long /api/chat.
        self._active_turn_tasks: dict[int, asyncio.Task[Any]] = {}
        # Stable transport request_id for the in-flight /api/chat of each chat.
        self._active_request_ids: dict[int, str] = {}
        # Last assistant answer per chat (for action chips: inbox / remind / more).
        self._last_answers: dict[int, str] = {}
        # Mid-turn progress message_id so we can delete/edit instead of spamming.
        self._progress_message_ids: dict[int, int] = {}

    def _initialize_bot_identity(self, me: object) -> None:
        actual_bot_id = me.get("id") if isinstance(me, dict) else None
        if (
            isinstance(actual_bot_id, bool)
            or not isinstance(actual_bot_id, int)
            or actual_bot_id <= 0
        ):
            raise RuntimeError("Telegram getMe did not return a valid immutable bot id")
        canonical_realm = f"telegram:{actual_bot_id}"
        if self.cfg.bot_id and self.cfg.bot_id != actual_bot_id:
            raise RuntimeError(
                "Telegram getMe identity does not match JARVIS_TELEGRAM_BOT_ID"
            )
        if self.cfg.realm_id and self.cfg.realm_id != canonical_realm:
            raise RuntimeError(
                "Telegram getMe identity does not match JARVIS_TELEGRAM_REALM_ID"
            )
        if self._conversation_store is not None and self._realm_id != canonical_realm:
            raise RuntimeError("Telegram bot identity changed after history store binding")
        self._bot_id = actual_bot_id
        self._realm_id = canonical_realm
        if self.cfg.conversation_store_path is not None and self._conversation_store is None:
            self._conversation_store = TelegramConversationStore(
                self.cfg.conversation_store_path,
                realm_id=canonical_realm,
                legacy_path=self.cfg.legacy_conversation_store_path,
                legacy_realm_id=self.cfg.legacy_conversation_realm_id,
                legacy_source_realm_id=(
                    self.cfg.legacy_conversation_source_realm_id
                ),
            )
            self._offset = self._conversation_store.next_update_offset()

    def _bot_identity(self) -> tuple[str, int]:
        if not self._realm_id or self._bot_id <= 0:
            raise RuntimeError("Telegram bot identity has not been established by getMe")
        return self._realm_id, self._bot_id

    async def aclose(self) -> None:
        self._closing = True
        tasks = tuple(self._update_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(self.tg.aclose(), self.api.aclose(), return_exceptions=True)

    def _consume_bridge_intake(self, chat_id: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        window = self._intake_windows.pop(chat_id, None)
        if window is None or current - window[0] >= 60:
            window = (current, 0)
        started_at, count = window
        count += 1
        self._intake_windows[chat_id] = (started_at, count)
        while len(self._intake_windows) > 10_000:
            self._intake_windows.popitem(last=False)
        return count <= max(1, self.cfg.intake_rate_per_minute)

    def _pre_provisioned_chat_ids(self) -> frozenset[int]:
        path = self.cfg.pre_provisioned_path
        if path is None:
            return frozenset()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._pre_provisioned_cache = frozenset()
            self._pre_provisioned_mtime = None
            return frozenset()
        if self._pre_provisioned_mtime == mtime:
            return self._pre_provisioned_cache
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            ids = frozenset(
                int(item)
                for item in (raw.get("chat_ids") or [])
                if str(item).lstrip("-").isdigit()
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            ids = frozenset()
        self._pre_provisioned_cache = ids
        self._pre_provisioned_mtime = mtime
        return ids

    def _chat_is_allowed(self, chat_id: int) -> bool:
        """Env allowlist (if set) OR admin pre-provisioned TG identity."""

        if not self.cfg.allowed_chat_ids:
            return True
        if chat_id in self.cfg.allowed_chat_ids:
            return True
        return chat_id in self._pre_provisioned_chat_ids()

    def _enqueue_update(self, update: dict, *, lease_token: str | None = None) -> bool:
        """Schedule a fair update turn or reject it before it can grow an unbounded queue."""

        callback = update.get("callback_query")
        if isinstance(callback, dict):
            message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            sender = callback.get("from") if isinstance(callback.get("from"), dict) else None
            chat_id = chat.get("id")
            if (
                isinstance(chat_id, bool)
                or not isinstance(chat_id, int)
                or not isinstance(sender, dict)
            ):
                log.warning("DENIED ambiguous Telegram callback_query before bridge queue")
                return False
            if not self._chat_is_allowed(chat_id):
                log.warning("DENIED Telegram user_id=%s by optional allowlist", chat_id)
                return False
            task = asyncio.create_task(
                self._handle_callback_query(chat_id, lease_token=lease_token, update=update)
            )
            self._update_tasks.add(task)
            task.add_done_callback(self._on_update_task_done)
            return True

        message = update.get("message")
        identity = self._telegram_identity(message) if isinstance(message, dict) else None
        if identity is None:
            log.warning("DENIED ambiguous Telegram update before bridge queue")
            return False
        chat_id = identity[0]
        owner_invite_proof = _owner_invite_proof_from_update(update)
        if not self._chat_is_allowed(chat_id) and owner_invite_proof is None:
            log.warning("DENIED Telegram user_id=%s by optional allowlist", chat_id)
            return False
        # /stop must not sit behind a long agent turn on the same chat lock.
        text = ""
        if isinstance(message, dict):
            text = str(message.get("text") or message.get("caption") or "")
        if _telegram_command(text) in _STOP_COMMANDS:
            task = asyncio.create_task(
                self._handle_stop_command(chat_id, lease_token=lease_token, update=update)
            )
            self._update_tasks.add(task)
            task.add_done_callback(self._on_update_task_done)
            return True
        if not self._consume_bridge_intake(chat_id):
            log.warning("THROTTLED Telegram user_id=%s at bridge intake", chat_id)
            return False
        if self._pending_per_chat.get(chat_id, 0) >= max(
            1, self.cfg.max_pending_per_user
        ):
            log.warning("BACKPRESSURE Telegram user_id=%s queue is full", chat_id)
            return False
        if len(self._update_tasks) >= max(1, self.cfg.max_pending_updates):
            log.warning("BACKPRESSURE Telegram global update queue is full")
            return False
        self._pending_per_chat[chat_id] = self._pending_per_chat.get(chat_id, 0) + 1
        task = asyncio.create_task(
            self._process_queued_update(chat_id, update, lease_token=lease_token)
        )
        self._update_tasks.add(task)
        task.add_done_callback(self._on_update_task_done)
        return True

    async def _backend_cancel_turn(self, chat_id: int) -> bool:
        """Ask the backend to abort the in-flight agent turn for this Telegram chat."""

        payload: dict[str, object] = {"notification_chat_id": int(chat_id)}
        request_id = self._active_request_ids.get(chat_id)
        if request_id:
            payload["request_id"] = request_id
        try:
            headers = self._session_headers(chat_id)
        except RuntimeError:
            headers = {}
        try:
            response = await self.api.post(
                "/api/chat/cancel",
                json=payload,
                headers=headers,
            )
            if response.status_code >= 400:
                return False
            body = response.json() if response.content else {}
            return bool(body.get("cancelled"))
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    async def _handle_stop_command(
        self,
        chat_id: int,
        *,
        lease_token: str | None,
        update: dict,
    ) -> None:
        """Cancel an in-flight agent turn for this chat without waiting on the chat lock."""

        terminal_status = "completed"
        error: str | None = None
        try:
            active = self._active_turn_tasks.get(chat_id)
            backend_cancelled = await self._backend_cancel_turn(chat_id)
            if active is not None and not active.done():
                active.cancel()
                with suppress(asyncio.CancelledError):
                    await active
                if backend_cancelled:
                    await self._send(
                        chat_id,
                        "Остановил. Backend-turn отменён — повторный результат не придёт.",
                    )
                else:
                    await self._send(
                        chat_id,
                        "Остановил ожидание ответа. Backend может ещё досчитать в фоне — "
                        "повторный результат проигнорирую, если придёт после /stop.",
                    )
            elif backend_cancelled:
                await self._send(chat_id, "Остановил backend-turn.")
            else:
                await self._send(chat_id, "Сейчас нет активного запроса.")
        except Exception as exc:  # noqa: BLE001
            terminal_status = "failed"
            error = type(exc).__name__
            log.exception("stop command failed for chat_id=%s", chat_id)
        finally:
            update_id = update.get("update_id")
            if (
                lease_token
                and self._conversation_store is not None
                and isinstance(update_id, int)
                and not isinstance(update_id, bool)
            ):
                self._conversation_store.finalize_update(
                    update_id,
                    lease_token,
                    status=terminal_status,
                    error=error,
                )

    async def _handle_callback_query(
        self,
        chat_id: int,
        *,
        lease_token: str | None,
        update: dict,
    ) -> None:
        """Handle reminder snooze/done inline buttons without waiting on the chat lock."""

        terminal_status = "completed"
        error: str | None = None
        try:
            callback = update.get("callback_query")
            if not isinstance(callback, dict):
                return
            callback_id = str(callback.get("id") or "")
            data = str(callback.get("data") or "").strip()
            sender = callback.get("from") if isinstance(callback.get("from"), dict) else None
            if callback_id:
                with suppress(httpx.HTTPError, RuntimeError):
                    await self._tg("answerCallbackQuery", callback_query_id=callback_id)
            if sender is None:
                return
            try:
                await self._open_user_session(
                    update_id=int(update.get("update_id") or 0),
                    chat_id=chat_id,
                    sender=sender,
                )
            except httpx.HTTPError:
                log.exception("callback session open failed for chat_id=%s", chat_id)
                return
            reply = await self._dispatch_callback_data(
                chat_id, data, update_id=int(update.get("update_id") or 0)
            )
            if reply:
                await self._send(chat_id, reply)
        except Exception as exc:  # noqa: BLE001
            terminal_status = "failed"
            error = type(exc).__name__
            log.exception("callback_query failed for chat_id=%s", chat_id)
        finally:
            update_id = update.get("update_id")
            if (
                lease_token
                and self._conversation_store is not None
                and isinstance(update_id, int)
                and not isinstance(update_id, bool)
            ):
                self._conversation_store.finalize_update(
                    update_id,
                    lease_token,
                    status=terminal_status,
                    error=error,
                )

    async def _dispatch_callback_data(
        self,
        chat_id: int,
        data: str,
        *,
        update_id: int,
    ) -> str | None:
        """Route inline callback payloads: reminder snooze or answer-action chips."""

        if data.startswith("r:"):
            return await self._dispatch_reminder_callback(chat_id, data)
        if data.startswith("a:"):
            return await self._dispatch_answer_action(
                chat_id, data, update_id=update_id
            )
        return None

    async def _dispatch_answer_action(
        self,
        chat_id: int,
        data: str,
        *,
        update_id: int,
    ) -> str | None:
        """Handle ``a:inbox`` / ``a:r60`` / ``a:more`` / ``a:stop`` chips."""

        action = data.split(":", 1)[-1] if ":" in data else data
        if action == "stop":
            backend_cancelled = await self._backend_cancel_turn(chat_id)
            active = self._active_turn_tasks.get(chat_id)
            if active is not None and not active.done():
                active.cancel()
                with suppress(asyncio.CancelledError):
                    await active
            if backend_cancelled:
                return "Остановил backend-turn."
            return "Сейчас нет активного запроса."
        last = (self._last_answers.get(chat_id) or "").strip()
        if action == "inbox":
            if not last:
                return "Нет последнего ответа, чтобы положить в inbox."
            await self._quick_capture(chat_id, f"из ответа: {last[:4000]}")
            return None  # _quick_capture already replied
        if action == "r60":
            if not last:
                return "Нет последнего ответа для напоминания."
            preview = last if len(last) <= 400 else last[:397] + "..."
            prompt = f"напомни через 1 час: {preview}"
            await self._run_turn(
                chat_id,
                prompt,
                [],
                request_id=f"{self._realm_id}:cb:{update_id}:r60",
            )
            return None
        if action == "more":
            await self._run_turn(
                chat_id,
                "Продолжи и углуби предыдущий ответ — добавь конкретику, без воды.",
                [],
                request_id=f"{self._realm_id}:cb:{update_id}:more",
            )
            return None
        return None

    async def _dispatch_reminder_callback(self, chat_id: int, data: str) -> str | None:
        """Map ``r:<reminder_id>:<action>`` callback data to backend snooze/ack."""

        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "r":
            return None
        reminder_id, action = parts[1], parts[2]
        if not reminder_id:
            return None
        try:
            headers = self._session_headers(chat_id)
        except RuntimeError:
            return "Нет сессии — напиши /start."
        try:
            if action in {"s10", "s60"}:
                minutes = 10 if action == "s10" else 60
                response = await self.api.post(
                    f"/api/reminders/{reminder_id}/snooze",
                    json={"minutes": minutes},
                    headers=headers,
                )
                if response.status_code == 404:
                    return "Напоминание уже недоступно."
                response.raise_for_status()
                body = response.json() if response.content else {}
                return str(body.get("detail") or f"Отложено на {minutes} мин.")
            if action == "ok":
                response = await self.api.post(
                    f"/api/reminders/{reminder_id}/ack",
                    headers=headers,
                )
                if response.status_code == 404:
                    return "Напоминание уже недоступно."
                response.raise_for_status()
                body = response.json() if response.content else {}
                return str(body.get("detail") or "Готово.")
        except httpx.HTTPError:
            log.exception("reminder callback API failed for %s", reminder_id)
            return "Не смог обработать кнопку — попробуй ещё раз."
        return None

    def _on_update_task_done(self, task: asyncio.Task[None]) -> None:
        self._update_tasks.discard(task)
        self._drain_durable_inbox()

    async def _process_queued_update(
        self,
        chat_id: int,
        update: dict,
        *,
        lease_token: str | None,
    ) -> None:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        terminal_status = "completed"
        error: str | None = None
        try:
            # Wait for this user's single-flight lock *before* consuming a global
            # worker slot, otherwise one user's queued turn can block another user.
            async with lock, self._update_slots:
                current = asyncio.current_task()
                if current is not None:
                    self._active_turn_tasks[chat_id] = current
                try:
                    outcome = await self._handle(update)
                finally:
                    if self._active_turn_tasks.get(chat_id) is current:
                        self._active_turn_tasks.pop(chat_id, None)
                if outcome is False:
                    terminal_status = "failed"
                    error = "transient_backend_failure"
        except asyncio.CancelledError:
            terminal_status = "pending"
            raise
        except Exception as exc:  # noqa: BLE001 - isolate one user's failed turn.
            terminal_status = "failed"
            error = type(exc).__name__
            log.exception("update %s failed", update.get("update_id"))
        finally:
            update_id = update.get("update_id")
            if (
                lease_token
                and self._conversation_store is not None
                and isinstance(update_id, int)
                and not isinstance(update_id, bool)
            ):
                self._conversation_store.finalize_update(
                    update_id,
                    lease_token,
                    status=terminal_status,
                    error=error,
                )
            remaining = self._pending_per_chat.get(chat_id, 1) - 1
            if remaining <= 0:
                self._pending_per_chat.pop(chat_id, None)
                if not lock.locked():
                    self._chat_locks.pop(chat_id, None)
            else:
                self._pending_per_chat[chat_id] = remaining
            self._drain_durable_inbox()

    def _drain_durable_inbox(self) -> None:
        if self._closing:
            return
        store = self._conversation_store
        if store is None:
            return
        capacity = max(0, self.cfg.max_pending_updates - len(self._update_tasks))
        if capacity <= 0:
            return
        claimed = store.claim_pending_updates(
            limit=capacity,
            lease_seconds=max(600, int(self.cfg.request_timeout) + 300),
        )
        for update, lease_token in claimed:
            if self._enqueue_update(update, lease_token=lease_token):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                store.finalize_update(
                    update_id,
                    lease_token,
                    status="rejected",
                    error="bridge_admission_denied",
                )

    def _conversation_for(
        self, chat_id: int, access_mode: str, *, user_id: str
    ) -> str:
        if self._conversation_store is not None:
            conversation_id = self._conversation_store.get_or_create(
                chat_id, access_mode, user_id=user_id
            )
        else:
            conversation_id = self.conversations.get(chat_id, "")
            if not conversation_id:
                conversation_id = TelegramConversationStore._new_conversation_id()
        self._cache_conversation(chat_id, conversation_id, access_mode)
        return conversation_id

    def _rotate_conversation(
        self, chat_id: int, access_mode: str, *, user_id: str
    ) -> str:
        if self._conversation_store is not None:
            conversation_id = self._conversation_store.rotate(
                chat_id, access_mode, user_id=user_id
            )
        else:
            conversation_id = TelegramConversationStore._new_conversation_id()
        self._cache_conversation(chat_id, conversation_id, access_mode)
        return conversation_id

    def _bind_conversation(
        self,
        chat_id: int,
        conversation_id: str,
        access_mode: str,
        *,
        user_id: str,
    ) -> None:
        for other_chat_id, bound_id in self.conversations.items():
            if other_chat_id != chat_id and bound_id == conversation_id:
                raise TelegramConversationIsolationError(
                    "backend conversation id is already bound to another Telegram chat"
                )
        if self._conversation_store is not None:
            self._conversation_store.bind(
                chat_id, conversation_id, access_mode, user_id=user_id
            )
        self._cache_conversation(chat_id, conversation_id, access_mode)

    def _cache_conversation(
        self, chat_id: int, conversation_id: str, access_mode: str
    ) -> None:
        self.conversations.pop(chat_id, None)
        self._conversation_modes.pop(chat_id, None)
        self.conversations[chat_id] = conversation_id
        self._conversation_modes[chat_id] = access_mode
        while len(self.conversations) > _BRIDGE_HOT_CACHE_SIZE:
            evicted_chat_id, _ = self.conversations.popitem(last=False)
            self._conversation_modes.pop(evicted_chat_id, None)

    def _cache_session(self, chat_id: int, session: TelegramUserSession) -> None:
        self._sessions.pop(chat_id, None)
        self._sessions[chat_id] = session
        while len(self._sessions) > _BRIDGE_HOT_CACHE_SIZE:
            self._sessions.popitem(last=False)

    @staticmethod
    def _telegram_identity(message: dict) -> tuple[int, dict] | None:
        """Return a cryptographically Telegram-bound principal for a private DM only.

        ``chat.id`` is not sufficient on its own: channel posts and anonymous admins can
        populate ``sender_chat`` instead of a real user. For a bot DM Telegram guarantees
        that the private chat id and immutable user id are equal, so require that binding.
        """

        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return None
        if message.get("sender_chat") is not None or chat.get("type") != "private":
            return None
        chat_id = chat.get("id")
        sender_id = sender.get("id")
        if (
            isinstance(chat_id, bool)
            or isinstance(sender_id, bool)
            or not isinstance(chat_id, int)
            or not isinstance(sender_id, int)
            or chat_id <= 0
            or chat_id != sender_id
            or sender.get("is_bot") is not False
        ):
            return None
        return sender_id, sender

    async def _open_user_session(
        self,
        *,
        update_id: int,
        chat_id: int,
        sender: Mapping[str, object],
        owner_invite_proof: str | None = None,
    ) -> TelegramUserSession | None:
        """Register/update the Telegram identity and obtain a scoped backend session.

        The backend atomically records ``update_id``. A conflict means the same update id
        reappeared with different identity/content and is ignored as a replay mismatch.
        """

        realm_id, bot_id = self._bot_identity()
        payload = {
            "realm_id": realm_id,
            "bot_id": bot_id,
            "update_id": update_id,
            "telegram_user": {
                "id": chat_id,
                "username": sender.get("username"),
                "first_name": sender.get("first_name"),
                "last_name": sender.get("last_name"),
                "language_code": sender.get("language_code"),
                "is_premium": bool(sender.get("is_premium", False)),
            },
            "chat": {"id": chat_id, "type": "private"},
        }
        if owner_invite_proof is not None:
            payload["owner_invite_proof"] = owner_invite_proof
        headers = {_BRIDGE_SECRET_HEADER: self.cfg.bridge_secret}
        existing_session = self._sessions.get(chat_id)
        if existing_session is not None:
            self._sessions.move_to_end(chat_id)
            headers[_USER_SESSION_HEADER] = existing_session.token
        response = await self.api.post(
            "/api/integrations/telegram/session",
            json=payload,
            headers=headers,
        )
        if response.status_code == 409:
            try:
                detail = str((response.json() or {}).get("detail") or "")
            except ValueError:
                detail = ""
            if detail == "Telegram update replay mismatch":
                log.warning("Ignored conflicting Telegram replay update_id=%s", update_id)
                return None
        response.raise_for_status()
        body = response.json()
        if (
            str(body.get("realm_id") or "").strip() != realm_id
            or body.get("bot_id") != bot_id
        ):
            raise ValueError("backend returned a mismatched Telegram realm identity")
        token = str(body.get("session_token") or "").strip()
        user = body.get("user") if isinstance(body.get("user"), dict) else {}
        user_id = str(body.get("user_id") or user.get("id") or "").strip()
        preset_key = str(
            body.get("preset_key")
            or user.get("preset_key")
            or user.get("preset")
            or "guest"
        ).strip()
        owner_invite_claimed = bool(user.get("owner_invite_claimed", False))
        if not token or not user_id:
            raise ValueError("backend returned an incomplete Telegram user session")
        session = TelegramUserSession(
            token=token,
            user_id=user_id,
            preset_key=preset_key,
            owner_invite_claimed=owner_invite_claimed,
        )
        self._cache_session(chat_id, session)
        return session

    def _session_headers(self, chat_id: int) -> dict[str, str]:
        session = self._sessions.get(chat_id)
        if session is None:
            raise RuntimeError("Telegram user session is not established")
        self._sessions.move_to_end(chat_id)
        return {_USER_SESSION_HEADER: session.token}

    # -- Telegram API ---------------------------------------------------------
    async def _tg(self, method: str, **params: object) -> object:
        response = await self.tg.post(
            f"/{method}", json={k: v for k, v in params.items() if v is not None}
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body}")
        return body.get("result")

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> None:
        """Send a reply as Telegram HTML (code blocks, bold, links, tables).

        The model answers in Markdown; we render it to Telegram's HTML subset and
        split it tag-safely. If Telegram still rejects a piece (malformed HTML), that
        piece is re-sent as plain text so the message always arrives.
        ``reply_markup`` (inline or reply keyboard) is attached to the *last* piece.
        """

        html = render_telegram_html(text)
        pieces = list(split_telegram_html(html))
        if not pieces:
            pieces = [" "]
        for index, piece in enumerate(pieces):
            markup = reply_markup if index == len(pieces) - 1 else None
            if await self._send_piece(chat_id, piece, html=True, reply_markup=markup):
                continue
            plain_parts = list(_chunks(html_to_plain(piece)))
            for p_index, plain in enumerate(plain_parts):
                plain_markup = (
                    markup if p_index == len(plain_parts) - 1 else None
                )
                await self._send_piece(
                    chat_id, plain, html=False, reply_markup=plain_markup
                )

    async def _send_piece(
        self,
        chat_id: int,
        text: str,
        *,
        html: bool,
        reply_markup: dict | None = None,
    ) -> bool:
        params: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if html:
            params["parse_mode"] = "HTML"
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        try:
            await self._tg("sendMessage", **params)
            return True
        except (httpx.HTTPError, RuntimeError):
            return False

    async def _typing_keepalive(self, chat_id: int) -> None:
        while True:
            with suppress(httpx.HTTPError, RuntimeError):
                await self._tg("sendChatAction", chat_id=chat_id, action="typing")
            await asyncio.sleep(TYPING_REFRESH_SEC)

    async def _progress_status(self, chat_id: int) -> None:
        """One mid-turn status ping so long Telegram turns do not feel dead.

        Sends a single editable message with a Stop chip; cleared when the turn ends
        so the chat is not permanently polluted with «Ещё работаю…».
        """

        try:
            await asyncio.sleep(_PROGRESS_STATUS_AFTER_SEC)
        except asyncio.CancelledError:
            raise
        with suppress(httpx.HTTPError, RuntimeError):
            result = await self._tg(
                "sendMessage",
                chat_id=chat_id,
                text="⏳ Ещё работаю… Отвечу, как закончу.",
                reply_markup=progress_stop_keyboard(),
                disable_web_page_preview=True,
            )
            if isinstance(result, dict):
                message_id = result.get("message_id")
                if isinstance(message_id, int) and not isinstance(message_id, bool):
                    self._progress_message_ids[chat_id] = message_id

    async def _clear_progress(self, chat_id: int) -> None:
        message_id = self._progress_message_ids.pop(chat_id, None)
        if message_id is None:
            return
        with suppress(httpx.HTTPError, RuntimeError):
            await self._tg("deleteMessage", chat_id=chat_id, message_id=message_id)

    # -- main loop ------------------------------------------------------------
    async def run(self) -> None:
        me = await self._tg("getMe")
        self._initialize_bot_identity(me)
        username = me.get("username") if isinstance(me, dict) else "?"
        log.info(
            "Telegram bridge online as @%s; allowlist=%s (%d id(s))",
            username,
            "enabled" if self.cfg.allowed_chat_ids else "disabled",
            len(self.cfg.allowed_chat_ids),
        )
        self._drain_durable_inbox()
        while True:
            try:
                updates = (
                    await self._tg(
                        "getUpdates",
                        offset=self._offset,
                        timeout=self.cfg.poll_timeout,
                        allowed_updates=["message", "callback_query"],
                    )
                    or []
                )
            except (httpx.HTTPError, RuntimeError):
                log.exception("getUpdates failed; backing off")
                await asyncio.sleep(3)
                continue
            next_offset = self._offset
            staged: list[tuple[int, int, dict]] = []
            for update in updates:
                update_id = update.get("update_id")
                if (
                    isinstance(update_id, bool)
                    or not isinstance(update_id, int)
                    or update_id < 0
                ):
                    log.warning("DENIED Telegram update with invalid update_id=%r", update_id)
                    continue
                next_offset = max(next_offset, update_id + 1)
                callback = update.get("callback_query")
                if isinstance(callback, dict):
                    message = (
                        callback.get("message")
                        if isinstance(callback.get("message"), dict)
                        else {}
                    )
                    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
                    chat_id = chat.get("id")
                    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
                        log.warning("DENIED ambiguous Telegram callback update_id=%s", update_id)
                        continue
                    if not self._chat_is_allowed(chat_id):
                        log.warning(
                            "DENIED Telegram user_id=%s by optional allowlist", chat_id
                        )
                        continue
                    # Callback identity is validated further in enqueue (from + chat).
                    staged.append((update_id, chat_id, update))
                    continue
                message = update.get("message")
                identity = (
                    self._telegram_identity(message) if isinstance(message, dict) else None
                )
                if identity is None:
                    log.warning("DENIED ambiguous Telegram update_id=%s", update_id)
                    continue
                chat_id = identity[0]
                owner_invite_proof = _owner_invite_proof_from_update(update)
                if not self._chat_is_allowed(chat_id) and owner_invite_proof is None:
                    log.warning("DENIED Telegram user_id=%s by optional allowlist", chat_id)
                    continue
                staged.append((update_id, chat_id, update))
            if self._conversation_store is not None:
                try:
                    self._conversation_store.persist_updates(staged)
                except sqlite3.Error:
                    log.exception("Could not durably stage Telegram updates; polling paused")
                    await asyncio.sleep(1)
                    continue
                # Telegram may now forget this batch: every accepted update is durable.
                self._offset = next_offset
                self._drain_durable_inbox()
            else:
                # Embedded/test mode has no durable inbox. Preserve at-least-once
                # acknowledgement by waiting for accepted work before advancing.
                batch_tasks_before = set(self._update_tasks)
                for _update_id, _chat_id, update in staged:
                    self._enqueue_update(update)
                batch_tasks = tuple(self._update_tasks - batch_tasks_before)
                if batch_tasks:
                    await asyncio.gather(*batch_tasks, return_exceptions=True)
                self._offset = next_offset

    async def _handle(self, update: dict) -> bool | None:
        if isinstance(update.get("callback_query"), dict):
            # Tests / embedded path: callbacks usually bypass the lock via enqueue.
            callback = update["callback_query"]
            message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            chat_id = chat.get("id")
            if isinstance(chat_id, bool) or not isinstance(chat_id, int):
                return
            await self._handle_callback_query(chat_id, lease_token=None, update=update)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        identity = self._telegram_identity(message)
        if identity is None:
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            log.warning(
                "DENIED ambiguous Telegram sender chat_id=%s type=%s",
                chat.get("id"),
                chat.get("type"),
            )
            return
        chat_id, sender = identity
        text = (message.get("text") or message.get("caption") or "").strip()
        owner_invite_proof = _owner_invite_proof_from_update(update)
        if not self._chat_is_allowed(chat_id) and owner_invite_proof is None:
            log.warning("DENIED Telegram user_id=%s by optional allowlist", chat_id)
            return
        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            log.warning("DENIED Telegram update with invalid update_id=%r", update_id)
            return
        try:
            session = await self._open_user_session(
                update_id=update_id,
                chat_id=chat_id,
                sender=sender,
                owner_invite_proof=owner_invite_proof,
            )
        except httpx.HTTPStatusError as exc:
            detail = ""
            with suppress(ValueError):
                detail = str((exc.response.json() or {}).get("detail") or "")
            if (
                owner_invite_proof is not None
                and exc.response.status_code == 403
                and detail == "Owner invitation is invalid or expired"
            ):
                await self._send(
                    chat_id,
                    "Owner-приглашение недействительно, уже использовано или истекло.",
                )
                return
            log.exception("Could not establish scoped Telegram session for user_id=%s", chat_id)
            if _retryable_backend_http_error(exc):
                return False
            raise
        except httpx.HTTPError as exc:
            log.exception("Could not establish scoped Telegram session for user_id=%s", chat_id)
            # The durable inbox retries the same immutable update id. The backend's
            # registration CAS makes an ambiguous lost response safe to replay.
            if _retryable_backend_http_error(exc):
                return False
            raise
        if session is None:
            return

        if owner_invite_proof is not None:
            if session.owner_invite_claimed and session.preset_key == "owner":
                await self._send(
                    chat_id,
                    "Готово: учётная запись создана или обновлена, статус owner активирован.",
                    reply_markup=operator_reply_keyboard(),
                )
            else:
                await self._send(
                    chat_id,
                    "Owner-приглашение уже не может быть использовано.",
                )
            return

        command = _telegram_command(text)
        console_action = _console_action_for_text(text)
        if console_action == "start" or command in _START_COMMANDS:
            await self._send(
                chat_id,
                _help_text(),
                reply_markup=operator_reply_keyboard(),
            )
            return
        if console_action in {
            "stop",
            "new",
            "help",
            "status",
            "briefing",
            "inbox_list",
            "quiet",
            "quiet_help",
        }:
            await self._handle_console_action(
                chat_id,
                console_action,
                session=session,
                update_id=update_id,
                raw_text=text,
            )
            return
        # /quiet 23:00-08:00 even when action map only saw the slash form.
        quiet_spec = _quiet_command_spec(text)
        if quiet_spec is not None:
            await self._handle_quiet_command(chat_id, quiet_spec)
            return
        if command in _STOP_COMMANDS:
            # Prefer the lock-bypassing enqueue path; this branch is a fallback when
            # _handle is invoked directly (tests / embedded mode).
            backend_cancelled = await self._backend_cancel_turn(chat_id)
            if backend_cancelled:
                await self._send(chat_id, "Остановил backend-turn.")
            else:
                await self._send(chat_id, "Сейчас нет активного запроса.")
            return
        if command in _RESET_COMMANDS:
            access_mode = "owner" if session.preset_key == "owner" else "guest"
            self._rotate_conversation(
                chat_id, access_mode, user_id=session.user_id
            )
            await self._send(
                chat_id,
                "Начал новый разговор.",
                reply_markup=operator_reply_keyboard(),
            )
            return

        capture_body = _quick_capture_body(text, command)
        if capture_body is not None:
            await self._quick_capture(chat_id, capture_body)
            return

        attachments = await self._ingest_inbound(chat_id, message)
        forwarded = _is_forwarded_message(message)
        if not text and not attachments and not forwarded:
            return
        audio_in = any(_looks_like_audio(a) for a in attachments)
        visual_in = any(not _looks_like_audio(a) for a in attachments)
        if forwarded:
            # Forward-as-task: any share into the bot becomes an actionable request.
            text = _build_forward_task_prompt(message, text)
        elif not text:
            # Voice-only note IS the message: a single space passes the API's non-empty gate
            # while the backend folds the audio transcript in as the real query; a
            # visual-only attachment gets a look-at-it nudge instead.
            text = " " if audio_in and not visual_in else "Посмотри на вложение и ответь."
        # Voice-out ONLY mirrors a spoken input; a text message always gets text back.
        return await self._run_turn(
            chat_id,
            text,
            attachments,
            voice_reply=audio_in and not forwarded,
            request_id=f"{self._realm_id}:{update_id}",
            # No per-answer action chips (Inbox / +1ч / Ещё) — they clutter every
            # reply. Reminder snooze/done buttons still attach only to fired reminders.
            action_chips=False,
        )

    # -- inbound files (photo/document -> /api/files/upload) ------------------
    async def _ingest_inbound(self, chat_id: int, message: dict) -> list[dict]:
        specs: list[tuple[str, str, str | None]] = []
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            biggest = max(photos, key=lambda p: p.get("file_size") or p.get("width") or 0)
            specs.append((biggest.get("file_id"), "photo.jpg", "image/jpeg"))
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            specs.append(
                (
                    document["file_id"],
                    document.get("file_name") or "file",
                    document.get("mime_type"),
                )
            )
        voice = message.get("voice")
        if isinstance(voice, dict) and voice.get("file_id"):
            specs.append((voice["file_id"], "voice.ogg", voice.get("mime_type") or "audio/ogg"))
        audio = message.get("audio")
        if isinstance(audio, dict) and audio.get("file_id"):
            specs.append(
                (
                    audio["file_id"],
                    audio.get("file_name") or "audio.mp3",
                    audio.get("mime_type") or "audio/mpeg",
                )
            )
        video_note = message.get("video_note")
        if isinstance(video_note, dict) and video_note.get("file_id"):
            specs.append((video_note["file_id"], "circle.mp4", "video/mp4"))
        attachments: list[dict] = []
        for file_id, name, mime in specs:
            try:
                record = await self._upload_from_telegram(chat_id, file_id, name, mime)
            except (httpx.HTTPError, RuntimeError, ValueError):
                log.exception("failed to relay inbound telegram file %s", file_id)
                continue
            if record:
                attachments.append(record)
        return attachments

    async def _upload_from_telegram(
        self,
        chat_id: int,
        file_id: str,
        name: str,
        mime: str | None,
    ) -> dict | None:
        info = await self._tg("getFile", file_id=file_id)
        file_path = info.get("file_path") if isinstance(info, dict) else None
        if not file_path:
            return None
        download = await self.tg.get(f"{self._tg_file_base}/{file_path}")
        download.raise_for_status()
        data = download.content
        if not data or len(data) > TG_DOC_CAP:
            return None
        upload = await self.api.post(
            "/api/files/upload",
            files={"file": (name, data, mime or "application/octet-stream")},
            headers=self._session_headers(chat_id),
        )
        upload.raise_for_status()
        item = (upload.json() or {}).get("file") or {}
        if not item.get("id"):
            return None
        return {
            "id": item["id"],
            "name": item.get("name") or name,
            "mime_type": item.get("mime_type") or mime,
            "size": item.get("size"),
        }

    # -- the agent turn -------------------------------------------------------
    async def _quick_capture(self, chat_id: int, body: str) -> None:
        """Dump a thought into durable memory without a full agent turn (GTD inbox)."""

        content = " ".join(str(body or "").split()).strip()
        if not content:
            await self._send(
                chat_id,
                "Что захватить? Пример: `/note купить молоко` или `+ идея про UI`.",
            )
            return
        try:
            response = await self.api.post(
                "/api/memory",
                json={
                    "content": content[:20000],
                    "namespace": "inbox",
                    "tags": ["capture", "telegram", "gtd"],
                    "importance": 0.6,
                },
                headers=self._session_headers(chat_id),
            )
            response.raise_for_status()
            item = response.json() if response.content else {}
        except httpx.HTTPError:
            log.exception("quick-capture memory save failed for chat_id=%s", chat_id)
            await self._send(chat_id, "Не смог сохранить в inbox — попробуй ещё раз.")
            return
        preview = content if len(content) <= 200 else content[:197] + "..."
        mem_id = str(item.get("id") or "").strip()
        suffix = f" (`{mem_id}`)" if mem_id else ""
        await self._send(chat_id, f"📥 В inbox{suffix}: {preview}")

    async def _handle_console_action(
        self,
        chat_id: int,
        action: str,
        *,
        session: TelegramUserSession,
        update_id: int,
        raw_text: str = "",
    ) -> None:
        """Day-console buttons and slash shortcuts that skip the full agent loop."""

        if action == "stop":
            backend_cancelled = await self._backend_cancel_turn(chat_id)
            active = self._active_turn_tasks.get(chat_id)
            if active is not None and not active.done():
                active.cancel()
                with suppress(asyncio.CancelledError):
                    await active
            if backend_cancelled:
                await self._send(chat_id, "Остановил backend-turn.")
            else:
                await self._send(chat_id, "Сейчас нет активного запроса.")
            return
        if action == "new":
            access_mode = "owner" if session.preset_key == "owner" else "guest"
            self._rotate_conversation(
                chat_id, access_mode, user_id=session.user_id
            )
            await self._send(
                chat_id,
                "Начал новый разговор.",
                reply_markup=operator_reply_keyboard(),
            )
            return
        if action == "help":
            await self._send(
                chat_id, _help_text(), reply_markup=operator_reply_keyboard()
            )
            return
        if action == "status":
            await self._send_status_card(chat_id)
            return
        if action == "briefing":
            await self._send_briefing_card(chat_id)
            return
        if action == "inbox_list":
            await self._send_inbox_preview(chat_id)
            return
        if action in {"quiet", "quiet_help"}:
            quiet_spec = _quiet_command_spec(raw_text)
            if quiet_spec is None:
                quiet_spec = "" if action == "quiet_help" else ""
            await self._handle_quiet_command(chat_id, quiet_spec or "")
            return

    async def _handle_quiet_command(self, chat_id: int, spec: str) -> None:
        """Show/set/clear operator quiet hours via preferences API."""

        from .notify import parse_quiet_hours

        try:
            headers = self._session_headers(chat_id)
        except RuntimeError:
            await self._send(chat_id, "Нет сессии — /start.")
            return
        # Bare /quiet or ⏰ → show current.
        if not spec or spec == "":
            try:
                response = await self.api.get("/api/preferences", headers=headers)
                response.raise_for_status()
                prefs = response.json() if response.content else {}
            except httpx.HTTPError:
                await self._send(chat_id, "Не смог прочитать preferences.")
                return
            current = str(prefs.get("quiet_hours") or "").strip()
            if current:
                await self._send(
                    chat_id,
                    f"🌙 Quiet hours: `{current}`\n"
                    "В окне напоминания копятся и уходят пачкой после тишины.\n"
                    "Сменить: `/quiet 23:00-08:00` · выкл: `/quiet off`",
                )
            else:
                await self._send(
                    chat_id,
                    "Quiet hours выключены.\n"
                    "Включить: `/quiet 23:00-08:00` (можно `23-8`).",
                )
            return
        if spec == "clear":
            patch = {"quiet_hours": ""}
        else:
            if parse_quiet_hours(spec) is None:
                await self._send(
                    chat_id,
                    "Не понял диапазон. Пример: `/quiet 23:00-08:00` или `/quiet off`.",
                )
                return
            patch = {"quiet_hours": spec}
        try:
            response = await self.api.patch(
                "/api/preferences", json=patch, headers=headers
            )
            response.raise_for_status()
            prefs = response.json() if response.content else {}
        except httpx.HTTPError:
            log.exception("quiet hours preferences update failed")
            await self._send(chat_id, "Не смог сохранить quiet hours.")
            return
        current = str(prefs.get("quiet_hours") or "").strip()
        if current:
            await self._send(
                chat_id,
                f"🌙 Quiet hours: `{current}`.\n"
                "Напоминания в этом окне отложу и пришлю пачкой после.",
            )
        else:
            await self._send(chat_id, "Quiet hours выключены.")

    async def _send_status_card(self, chat_id: int) -> None:
        try:
            response = await self.api.get(
                "/api/status", headers=self._session_headers(chat_id)
            )
            response.raise_for_status()
            body = response.json() if response.content else {}
        except httpx.HTTPError:
            await self._send(chat_id, "Не смог получить статус runtime.")
            return
        await self._send(chat_id, _format_status_card(body))

    async def _send_briefing_card(self, chat_id: int) -> None:
        try:
            response = await self.api.get(
                "/api/briefing", headers=self._session_headers(chat_id)
            )
            response.raise_for_status()
            body = response.json() if response.content else {}
        except httpx.HTTPError:
            await self._send(chat_id, "Не смог получить сводку.")
            return
        await self._send(chat_id, _format_briefing_card(body))

    async def _send_inbox_preview(self, chat_id: int) -> None:
        try:
            response = await self.api.get(
                "/api/memory",
                params={"q": "namespace:inbox OR capture", "limit": 8},
                headers=self._session_headers(chat_id),
            )
            # Fallback: unfiltered recent memory if query syntax is unsupported.
            if response.status_code >= 400:
                response = await self.api.get(
                    "/api/memory",
                    params={"limit": 12},
                    headers=self._session_headers(chat_id),
                )
            response.raise_for_status()
            items = response.json() if response.content else []
        except httpx.HTTPError:
            await self._send(chat_id, "Не смог прочитать inbox.")
            return
        if not isinstance(items, list):
            items = []
        inbox = [
            item
            for item in items
            if isinstance(item, dict)
            and (
                str(item.get("namespace") or "") == "inbox"
                or "capture" in [str(t) for t in (item.get("tags") or [])]
                or "gtd" in [str(t) for t in (item.get("tags") or [])]
            )
        ][:6]
        if not inbox:
            # Prefer anything recent if namespace filter empty.
            inbox = [item for item in items if isinstance(item, dict)][:5]
        if not inbox:
            await self._send(
                chat_id,
                "Inbox пуст. Добавь: `/note мысль` или `+ идея`.",
            )
            return
        lines = ["📥 Inbox:"]
        for item in inbox:
            content = " ".join(str(item.get("content") or "").split())
            if len(content) > 120:
                content = content[:117] + "..."
            lines.append(f"• {content}")
        await self._send(chat_id, "\n".join(lines))

    async def _run_turn(
        self,
        chat_id: int,
        text: str,
        attachments: list[dict],
        *,
        voice_reply: bool = False,
        request_id: str | None = None,
        action_chips: bool = False,
    ) -> bool | None:
        session = self._sessions[chat_id]
        before = await self._file_ids(chat_id)
        typing = asyncio.create_task(self._typing_keepalive(chat_id))
        body: dict = {}
        if request_id:
            self._active_request_ids[chat_id] = request_id
        try:
            access_mode = "owner" if session.preset_key == "owner" else "guest"
            conversation_id = self._conversation_for(
                chat_id, access_mode, user_id=session.user_id
            )
            payload: dict[str, object] = {
                "message": text,
                "conversation_id": conversation_id,
                # Stamp the Telegram chat so reminders/scheduled tasks fire back here.
                "notification_chat_id": int(chat_id),
                "response_modality": "voice" if voice_reply else "text",
            }
            if request_id:
                payload["request_id"] = request_id
            if attachments:
                payload["attachments"] = attachments
            progress_task = asyncio.create_task(
                self._progress_status(chat_id),
                name=f"tg-progress-{chat_id}",
            )
            try:
                response = await self.api.post(
                    "/api/chat",
                    json=payload,
                    headers=self._session_headers(chat_id),
                )
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPError as exc:
                log.exception("backend /api/chat failed")
                # ``request_id`` is stable for this Telegram update, so retrying an
                # ambiguous response cannot execute a second logical agent turn.
                if _retryable_backend_http_error(exc):
                    return False
                raise
            except asyncio.CancelledError:
                # /stop cancelled the bridge wait; backend cancel was already requested.
                raise
            finally:
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task
                await self._clear_progress(chat_id)
        finally:
            if self._active_request_ids.get(chat_id) == request_id:
                self._active_request_ids.pop(chat_id, None)
            typing.cancel()
            with suppress(asyncio.CancelledError):
                await typing

        returned_conversation_id = str(body.get("conversation_id") or "").strip()
        if returned_conversation_id and returned_conversation_id != conversation_id:
            try:
                self._bind_conversation(
                    chat_id,
                    returned_conversation_id,
                    access_mode,
                    user_id=session.user_id,
                )
            except TelegramConversationIsolationError:
                log.exception(
                    "refusing cross-chat backend conversation binding for chat_id=%s",
                    chat_id,
                )
                await self._send(
                    chat_id,
                    "Не смог безопасно продолжить диалог. Начни новый через /new.",
                )
                return
        answer = body.get("answer") or "(пустой ответ)"
        self._last_answers[chat_id] = str(answer)
        markup = answer_action_keyboard() if action_chips else None
        voice_sent = False
        if voice_reply and self.cfg.voice_replies:
            voice_sent = await self._reply_with_voice(chat_id, answer)
        if not voice_sent:
            await self._send(chat_id, answer, reply_markup=markup)

        inbound_ids = {a["id"] for a in attachments}
        await self._deliver_new_files(chat_id, before | inbound_ids)

    async def _reply_with_voice(self, chat_id: int, answer: str) -> bool:
        """Speak the answer back as a Telegram voice note (spoken input → spoken reply)."""

        text = (answer or "").strip()
        if not text or len(text) > self.cfg.voice_reply_max_chars:
            return False
        try:
            response = await self.api.post(
                "/api/voice/speak",
                json={"text": text},
                headers=self._session_headers(chat_id),
            )
            if response.status_code != 200 or not response.content:
                return False
            wav = response.content
        except httpx.HTTPError:
            return False
        ogg = await asyncio.to_thread(_wav_to_ogg_opus, wav)
        try:
            if ogg:
                sent = await self.tg.post(
                    "/sendVoice",
                    data={"chat_id": chat_id},
                    files={"voice": ("jarvis.ogg", ogg, "audio/ogg")},
                )
            else:  # no ffmpeg — fall back to a playable audio file
                sent = await self.tg.post(
                    "/sendAudio",
                    data={"chat_id": chat_id},
                    files={"audio": ("jarvis.wav", wav, "audio/wav")},
                )
            sent.raise_for_status()
            body = sent.json()
            return bool(body.get("ok"))
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False

    async def _file_ids(self, chat_id: int) -> set[str]:
        try:
            response = await self.api.get(
                "/api/files",
                params={"limit": 200},
                headers=self._session_headers(chat_id),
            )
            response.raise_for_status()
            return {item["id"] for item in response.json() if item.get("id")}
        except (httpx.HTTPError, KeyError, TypeError):
            return set()

    async def _deliver_new_files(self, chat_id: int, known: set[str]) -> None:
        try:
            response = await self.api.get(
                "/api/files",
                params={"limit": 200},
                headers=self._session_headers(chat_id),
            )
            response.raise_for_status()
            items = response.json()
        except (httpx.HTTPError, ValueError):
            return
        fresh = [
            item
            for item in items
            if isinstance(item, dict) and item.get("id") and item["id"] not in known
        ]
        for item in fresh[: self.cfg.max_files_out]:
            with suppress(httpx.HTTPError, RuntimeError):
                await self._send_document(chat_id, item)

    async def _send_document(self, chat_id: int, item: dict) -> None:
        size = item.get("size") or 0
        if size and size > TG_DOC_CAP:
            await self._send(chat_id, f"Файл {item.get('name')} слишком большой для Telegram.")
            return
        download = await self.api.get(
            f"/api/files/{item['id']}/download",
            headers=self._session_headers(chat_id),
        )
        download.raise_for_status()
        name = str(item.get("name") or "file")
        mime = str(item.get("mime_type") or "application/octet-stream")
        content = download.content
        # Images open inline on the phone; documents require an extra tap.
        if _looks_like_image(name, mime) and (not size or size <= _TG_PHOTO_CAP):
            photo_mime = mime if mime.startswith("image/") else "image/jpeg"
            await self.tg.post(
                "/sendPhoto",
                data={"chat_id": chat_id},
                files={"photo": (name, content, photo_mime)},
            )
            return
        await self.tg.post(
            "/sendDocument",
            data={"chat_id": chat_id},
            files={
                "document": (
                    name,
                    content,
                    mime,
                )
            },
        )


async def _amain() -> None:
    load_local_env_file()
    cfg = load_config()
    _configure_logging(cfg.bot_token)
    bridge = TelegramBridge(cfg)
    try:
        await bridge.run()
    finally:
        await bridge.aclose()


def run() -> None:
    """Entry point for ``python -m jarvis_gpt.telegram_bridge`` / the CLI subcommand."""

    with suppress(KeyboardInterrupt):
        asyncio.run(_amain())


if __name__ == "__main__":
    run()
