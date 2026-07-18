"""Telegram bot frontend for Jarvis.

A standalone long-lived process (NOT part of the FastAPI app) that relays the owner's
Telegram DMs to the same Jarvis agent through the backend HTTP API. Same brain, tools and
memory as the web UI — Jarvis in your pocket.

Security is non-negotiable and fail-closed: it only answers chat ids on the
``TELEGRAM_ALLOWED_CHAT_IDS`` allowlist in private chats, and it refuses to start without a
token and a non-empty allowlist. A denied update never reaches the agent.

Talks to Telegram over the raw Bot HTTP API with httpx long-polling (no aiogram / PTB
dependency — httpx is already a core dep) and to the backend as an ordinary API client.

Run it alongside the backend:  ``py -3.11 jarvis.py telegram-bridge``
Owner must set in backend/.env.local:
  TELEGRAM_BOT_TOKEN=<@BotFather token>
  TELEGRAM_ALLOWED_CHAT_IDS=<your numeric chat id>   # comma-separated for more than one
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import load_local_env_file
from .telegram_format import html_to_plain, render_telegram_html, split_telegram_html

log = logging.getLogger("jarvis.telegram")

TG_MSG_LIMIT = 4096
# Telegram bots can send documents up to 50 MB; guard a little under that.
TG_DOC_CAP = 45 * 1024 * 1024
TYPING_REFRESH_SEC = 4.0
_RESET_COMMANDS = {"/start", "/new", "/reset", "/новый", "/сброс"}

# Audio/video attachments are transcribed backend-side; the bridge only relays them and,
# for a spoken turn, mirrors the modality by replying with a synthesized voice note.
_AUDIO_MIME_PREFIXES = ("audio/", "video/")
_AUDIO_EXTENSIONS = frozenset(
    {
        ".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".wma",
        ".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi",
    }
)


def _looks_like_audio(attachment: Mapping[str, object]) -> bool:
    mime = str(attachment.get("mime_type") or "").lower()
    if mime.startswith(_AUDIO_MIME_PREFIXES):
        return True
    name = str(attachment.get("name") or "").lower()
    return Path(name).suffix in _AUDIO_EXTENSIONS


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
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0", "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", "pipe:1",
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
    api_token: str = ""
    poll_timeout: int = 25
    request_timeout: float = 300.0  # agent turns (missions, web, vision) can be long
    max_files_out: int = 6
    # Voice-out mirrors the input modality: a spoken reply is sent ONLY when the incoming
    # message was itself voice/audio. A text message always gets a text-only reply.
    voice_replies: bool = True
    voice_reply_max_chars: int = 1500


def load_config(env: Mapping[str, str] | None = None) -> TelegramConfig:
    """Build the config, failing CLOSED when the token or allowlist is missing."""

    env = os.environ if env is None else env
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set — create a bot with @BotFather and put the "
            "token in backend/.env.local. Refusing to start."
        )
    ids = frozenset(
        int(part)
        for part in re.split(r"[,\s]+", (env.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip())
        if part
    )
    if not ids:
        raise SystemExit(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty — set your numeric Telegram chat id in "
            "backend/.env.local. Refusing to start open to the world."
        )
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
    return TelegramConfig(
        bot_token=token,
        allowed_chat_ids=ids,
        backend_url=(env.get("JARVIS_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/"),
        api_token=(env.get("JARVIS_API_TOKEN") or "").strip(),
        voice_replies=voice_replies,
        voice_reply_max_chars=voice_max,
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


class TelegramBridge:
    def __init__(
        self,
        cfg: TelegramConfig,
        *,
        tg_client: httpx.AsyncClient | None = None,
        api_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
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
        self.conversations: dict[int, str] = {}
        self._offset = 0

    async def aclose(self) -> None:
        await asyncio.gather(self.tg.aclose(), self.api.aclose(), return_exceptions=True)

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

    async def _send(self, chat_id: int, text: str) -> None:
        """Send a reply as Telegram HTML (code blocks, bold, links, tables).

        The model answers in Markdown; we render it to Telegram's HTML subset and
        split it tag-safely. If Telegram still rejects a piece (malformed HTML), that
        piece is re-sent as plain text so the message always arrives.
        """

        html = render_telegram_html(text)
        for piece in split_telegram_html(html):
            if await self._send_piece(chat_id, piece, html=True):
                continue
            for plain in _chunks(html_to_plain(piece)):
                await self._send_piece(chat_id, plain, html=False)

    async def _send_piece(self, chat_id: int, text: str, *, html: bool) -> bool:
        params: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if html:
            params["parse_mode"] = "HTML"
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

    # -- main loop ------------------------------------------------------------
    async def run(self) -> None:
        me = await self._tg("getMe")
        username = me.get("username") if isinstance(me, dict) else "?"
        log.info(
            "Telegram bridge online as @%s; %d allowed chat(s)",
            username,
            len(self.cfg.allowed_chat_ids),
        )
        while True:
            try:
                updates = (
                    await self._tg(
                        "getUpdates",
                        offset=self._offset,
                        timeout=self.cfg.poll_timeout,
                        allowed_updates=["message"],
                    )
                    or []
                )
            except (httpx.HTTPError, RuntimeError):
                log.exception("getUpdates failed; backing off")
                await asyncio.sleep(3)
                continue
            for update in updates:
                # Advance the offset for EVERY update (even denied) or it repeats forever.
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                try:
                    await self._handle(update)
                except Exception:  # noqa: BLE001 - one bad update must not kill the loop
                    log.exception("update %s failed", update.get("update_id"))

    async def _handle(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        # SECURITY GATE — must run before any agent / backend call.
        if chat_id not in self.cfg.allowed_chat_ids or chat.get("type") != "private":
            log.warning("DENIED telegram chat_id=%s type=%s", chat_id, chat.get("type"))
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        if text in _RESET_COMMANDS:
            self.conversations.pop(chat_id, None)
            ack = "Джарвис на связи." if text in {"/start"} else "Начал новый разговор."
            await self._send(chat_id, ack)
            return

        attachments = await self._ingest_inbound(message)
        if not text and not attachments:
            return
        audio_in = any(_looks_like_audio(a) for a in attachments)
        visual_in = any(not _looks_like_audio(a) for a in attachments)
        if not text:
            # Voice-only note IS the message: a single space passes the API's non-empty gate
            # while the backend folds the audio transcript in as the real query; a
            # visual-only attachment gets a look-at-it nudge instead.
            text = " " if audio_in and not visual_in else "Посмотри на вложение и ответь."
        # Voice-out ONLY mirrors a spoken input; a text message always gets text back.
        await self._run_turn(chat_id, text, attachments, voice_reply=audio_in)

    # -- inbound files (photo/document -> /api/files/upload) ------------------
    async def _ingest_inbound(self, message: dict) -> list[dict]:
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
                record = await self._upload_from_telegram(file_id, name, mime)
            except (httpx.HTTPError, RuntimeError, ValueError):
                log.exception("failed to relay inbound telegram file %s", file_id)
                continue
            if record:
                attachments.append(record)
        return attachments

    async def _upload_from_telegram(
        self, file_id: str, name: str, mime: str | None
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
    async def _run_turn(
        self, chat_id: int, text: str, attachments: list[dict], *, voice_reply: bool = False
    ) -> None:
        before = await self._file_ids()
        typing = asyncio.create_task(self._typing_keepalive(chat_id))
        try:
            payload: dict[str, object] = {"message": text}
            conversation_id = self.conversations.get(chat_id)
            if conversation_id:
                payload["conversation_id"] = conversation_id
            if attachments:
                payload["attachments"] = attachments
            try:
                response = await self.api.post("/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPError:
                log.exception("backend /api/chat failed")
                await self._send(chat_id, "Не смог достучаться до ядра Джарвиса. Попробуй ещё раз.")
                return
        finally:
            typing.cancel()
            with suppress(asyncio.CancelledError):
                await typing

        if body.get("conversation_id"):
            self.conversations[chat_id] = body["conversation_id"]
        answer = body.get("answer") or "(пустой ответ)"
        await self._send(chat_id, answer)
        if voice_reply and self.cfg.voice_replies:
            await self._reply_with_voice(chat_id, answer)

        inbound_ids = {a["id"] for a in attachments}
        await self._deliver_new_files(chat_id, before | inbound_ids)

    async def _reply_with_voice(self, chat_id: int, answer: str) -> None:
        """Speak the answer back as a Telegram voice note (spoken input → spoken reply)."""

        text = (answer or "").strip()
        if not text or len(text) > self.cfg.voice_reply_max_chars:
            return
        try:
            response = await self.api.post("/api/voice/speak", json={"text": text})
            if response.status_code != 200 or not response.content:
                return
            wav = response.content
        except httpx.HTTPError:
            return
        ogg = await asyncio.to_thread(_wav_to_ogg_opus, wav)
        with suppress(httpx.HTTPError, RuntimeError):
            if ogg:
                await self.tg.post(
                    "/sendVoice",
                    data={"chat_id": chat_id},
                    files={"voice": ("jarvis.ogg", ogg, "audio/ogg")},
                )
            else:  # no ffmpeg — fall back to a playable audio file
                await self.tg.post(
                    "/sendAudio",
                    data={"chat_id": chat_id},
                    files={"audio": ("jarvis.wav", wav, "audio/wav")},
                )

    async def _file_ids(self) -> set[str]:
        try:
            response = await self.api.get("/api/files", params={"limit": 200})
            response.raise_for_status()
            return {item["id"] for item in response.json() if item.get("id")}
        except (httpx.HTTPError, KeyError, TypeError):
            return set()

    async def _deliver_new_files(self, chat_id: int, known: set[str]) -> None:
        try:
            response = await self.api.get("/api/files", params={"limit": 200})
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
        download = await self.api.get(f"/api/files/{item['id']}/download")
        download.raise_for_status()
        await self.tg.post(
            "/sendDocument",
            data={"chat_id": chat_id},
            files={
                "document": (
                    item.get("name") or "file",
                    download.content,
                    item.get("mime_type") or "application/octet-stream",
                )
            },
        )


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_local_env_file()
    bridge = TelegramBridge(load_config())
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
