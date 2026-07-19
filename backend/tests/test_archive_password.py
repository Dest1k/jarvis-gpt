"""Password-protected archive handling (ZIP) — missing / wrong / ok paths."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from jarvis_gpt.agent import (
    _extract_archive_password,
    _looks_like_bare_archive_password_reply,
    _looks_like_password_claim_without_archive,
)
from jarvis_gpt.archive_runtime import (
    ArchivePasswordError,
    extract_archive,
    list_archive,
    read_archive_member,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


_LIVE_PWD_FIXTURE = Path(r"D:\jarvis\data\jarvis-gpt\files\live_smoke_pwd.zip")
_LIVE_PWD = "secret42"
_LIVE_MEMBER = "_secret.txt"


def _encrypted_zip_fixture(dest: Path) -> tuple[Path, str, str]:
    """Return (path, password, member) for a ZipCrypto-encrypted archive."""

    dest.parent.mkdir(parents=True, exist_ok=True)
    if _LIVE_PWD_FIXTURE.exists():
        dest.write_bytes(_LIVE_PWD_FIXTURE.read_bytes())
        return dest, _LIVE_PWD, _LIVE_MEMBER
    try:
        import pyminizip  # type: ignore
    except ImportError:
        pyminizip = None
    if pyminizip is None:
        raise pytest.skip(
            "No encrypted ZIP fixture (live_smoke_pwd.zip / pyminizip unavailable)"
        )
    plain = dest.with_suffix(".plain.txt")
    plain.write_bytes(b"pwd_payload_99")
    pyminizip.compress(str(plain), None, str(dest), _LIVE_PWD, 5)
    plain.unlink(missing_ok=True)
    return dest, _LIVE_PWD, _LIVE_MEMBER


def test_list_requires_password_when_encrypted(tmp_path: Path):
    archive_path, _password, _member = _encrypted_zip_fixture(tmp_path / "data.zip")
    with pytest.raises(ArchivePasswordError) as raised:
        list_archive(archive_path)
    assert raised.value.reason == "missing"


def test_list_rejects_wrong_password(tmp_path: Path):
    archive_path, _password, _member = _encrypted_zip_fixture(tmp_path / "data.zip")
    with pytest.raises(ArchivePasswordError) as raised:
        list_archive(archive_path, password="nope")
    assert raised.value.reason == "wrong"


def test_list_and_extract_with_password_param(tmp_path: Path):
    archive_path, password, _member = _encrypted_zip_fixture(tmp_path / "data.zip")
    listing = list_archive(archive_path, password=password)
    assert listing["ok"] is True
    assert listing["member_count"] >= 1
    out = tmp_path / "out"
    result = extract_archive(archive_path, output_dir=out, password=password)
    assert result["extracted_count"] >= 1


def test_read_member_password_param(tmp_path: Path):
    archive_path, password, member = _encrypted_zip_fixture(tmp_path / "data.zip")
    payload = read_archive_member(archive_path, member, password=password)
    assert payload["ok"] is True
    assert payload["size"] > 0
    assert "pwd_payload" in str(payload.get("text_preview") or "")


def test_tools_surface_missing_and_wrong_password(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    # Must live under JARVIS_HOME so path policy accepts it.
    archive_path, good, _member = _encrypted_zip_fixture(home / "files" / "pwd.zip")
    path = str(archive_path)

    import asyncio

    missing = asyncio.run(tools.run("documents.archive.list", {"path": path}))
    assert missing.ok is False
    assert missing.data.get("archive_auth_gate") == "missing"
    assert missing.data.get("needs_auth") is True
    assert missing.data.get("archive_auth_required") is True

    wrong = asyncio.run(
        tools.run("documents.archive.list", {"path": path, "password": "nope"})
    )
    assert wrong.ok is False
    assert wrong.data.get("archive_auth_gate") == "wrong"
    assert wrong.data.get("wrong_auth") is True

    ok = asyncio.run(
        tools.run("documents.archive.list", {"path": path, "password": good})
    )
    assert ok.ok is True
    assert ok.data.get("member_count", 0) >= 1
    storage.close()


def test_password_extractors():
    assert _extract_archive_password("пароль secret42") == "secret42"
    assert _extract_archive_password("password: s3cret!") == "s3cret!"
    assert _extract_archive_password("открой архив") is None
    assert _looks_like_bare_archive_password_reply("secret42") is True
    assert _looks_like_bare_archive_password_reply("открой архив") is False
    assert _looks_like_password_claim_without_archive("пароль есть") is True
    assert _looks_like_password_claim_without_archive("пароль secret") is False
