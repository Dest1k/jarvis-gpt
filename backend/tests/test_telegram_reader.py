from __future__ import annotations

import json
import sys
import textwrap
from types import SimpleNamespace

import jarvis_gpt.telegram_reader as telegram_reader_module
import pytest
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.telegram_reader import (
    SubprocessTelegramAuthorizedReader,
    TelegramReaderCommandError,
    load_authorized_reader_from_environment,
)
from jarvis_gpt.telegram_sources import TelegramReaderSource
from jarvis_gpt.tools import ToolRegistry


def _reader_script(tmp_path):
    script = tmp_path / "reader_fixture.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys

            request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            if request["operation"] == "capability":
                response = {
                    "provider_name": "existing_cli",
                    "reader_identity_sha256": "a" * 64,
                    "configured": True,
                    "authenticated": True,
                    "state": ",".join(sorted(
                        key for key in os.environ
                        if key.startswith("JARVIS_") or key == "OPENAI_API_KEY"
                    )) or "ready",
                    "supports_history": True,
                    "supports_media": True,
                    "source_types": ["channel", "supergroup"],
                    "access_scopes": ["public", "private"],
                }
            else:
                response = {
                    "posts": [{
                        "message_id": 77,
                        "text": "機密資料 and 비밀 기록",
                        "date": "2026-07-20T10:00:00+00:00",
                        "version_id": "v1",
                        "permalink": "https://t.me/c/123/77",
                        "media": [{
                            "kind": "document",
                            "stable_id": "opaque-media-id",
                            "file_name": "report.pdf",
                            "mime_type": "application/pdf",
                            "size": 1234,
                        }],
                    }],
                    "complete": False,
                    "next_before_message_id": 77,
                }
            sys.stdout.buffer.write(json.dumps(response).encode("utf-8"))
            """
        ),
        encoding="utf-8",
    )
    return script


def test_subprocess_reader_uses_existing_session_boundary_without_jarvis_secrets(
    tmp_path, monkeypatch
):
    script = _reader_script(tmp_path)
    monkeypatch.setenv("JARVIS_API_TOKEN", "must-not-reach-reader")
    monkeypatch.setenv("JARVIS_TELEGRAM_BRIDGE_SECRET", "must-not-reach-reader")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-reader")
    reader = SubprocessTelegramAuthorizedReader((sys.executable, str(script)))

    capability = reader.capability()
    batch = reader.read_history(
        TelegramReaderSource(
            realm_id="telegram:777001",
            source_chat_id=-100123,
            source_type="supergroup",
            access_scope="private",
        ),
        limit=500,
    )

    assert capability.authenticated is True
    assert capability.state == "ready"
    assert batch.complete is False
    assert batch.next_before_message_id == 77
    assert batch.posts[0].text == "機密資料 and 비밀 기록"
    assert batch.posts[0].media[0].stable_id == "opaque-media-id"


def test_environment_loader_requires_json_argv_with_absolute_executable(tmp_path, monkeypatch):
    script = _reader_script(tmp_path)
    monkeypatch.setenv(
        "JARVIS_TELEGRAM_READER_COMMAND_JSON",
        json.dumps([sys.executable, str(script)]),
    )

    reader = load_authorized_reader_from_environment()

    assert isinstance(reader, SubprocessTelegramAuthorizedReader)
    assert reader.capability().provider_name == "existing_cli"

    monkeypatch.setenv("JARVIS_TELEGRAM_READER_COMMAND_JSON", '["relative.exe"]')
    assert load_authorized_reader_from_environment() is None


def test_tool_registry_loads_configured_external_reader_for_production_runtime(
    tmp_path, monkeypatch
):
    script = _reader_script(tmp_path)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv(
        "JARVIS_TELEGRAM_READER_COMMAND_JSON",
        json.dumps([sys.executable, str(script)]),
    )
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    registry = ToolRegistry(
        settings,
        storage,
        SimpleNamespace(complete=None),
    )

    assert registry.telegram_sources is not None
    reader = registry.telegram_sources._authorized_reader
    assert isinstance(reader, SubprocessTelegramAuthorizedReader)
    assert reader.capability().authenticated is True
    storage.close()


@pytest.mark.parametrize("message_id", [True, "77", 0, -1])
def test_subprocess_reader_rejects_non_integer_message_identity(
    tmp_path,
    monkeypatch,
    message_id,
):
    reader = SubprocessTelegramAuthorizedReader(
        (sys.executable, str(_reader_script(tmp_path)))
    )
    monkeypatch.setattr(
        reader,
        "_invoke",
        lambda _operation, _payload: {
            "posts": [
                {
                    "message_id": message_id,
                    "text": "evidence",
                    "date": "2026-07-20T10:00:00+00:00",
                }
            ],
            "complete": True,
        },
    )

    with pytest.raises(TelegramReaderCommandError, match="message id"):
        reader.read_history(
            TelegramReaderSource(
                realm_id="telegram:777001",
                source_chat_id=-100123,
                source_type="channel",
                access_scope="public",
            ),
            limit=500,
        )


def test_subprocess_reader_rejects_cursor_for_complete_page(tmp_path, monkeypatch):
    reader = SubprocessTelegramAuthorizedReader(
        (sys.executable, str(_reader_script(tmp_path)))
    )
    monkeypatch.setattr(
        reader,
        "_invoke",
        lambda _operation, _payload: {
            "posts": [],
            "complete": True,
            "next_before_message_id": 77,
        },
    )

    with pytest.raises(TelegramReaderCommandError, match="complete history"):
        reader.read_history(
            TelegramReaderSource(
                realm_id="telegram:777001",
                source_chat_id=-100123,
                source_type="channel",
                access_scope="public",
            ),
            limit=500,
        )


def test_subprocess_reader_rejects_oversized_stdout_without_returning_content(
    tmp_path,
    monkeypatch,
):
    reader = SubprocessTelegramAuthorizedReader(
        (sys.executable, str(_reader_script(tmp_path)))
    )
    monkeypatch.setattr(telegram_reader_module, "_MAX_RESPONSE_BYTES", 64)

    with pytest.raises(TelegramReaderCommandError, match="command failed"):
        reader.capability()
