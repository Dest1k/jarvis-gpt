"""The custom multithreaded/resumable HF model downloader — driven entirely through
httpx.MockTransport, so the segmentation, resume and verification logic is proven with
no network and no huggingface_hub."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from jarvis_gpt.model_downloader import (
    ModelDownloadError,
    RemoteFile,
    _plan_segments,
    _segment_meta_path,
    download_file,
    download_model,
    parse_tree_entries,
    read_hf_token,
    resolve_url,
    verify_file,
)

_BLOB = bytes(range(256)) * 400  # 102_400 bytes of deterministic content
_SHA = hashlib.sha256(_BLOB).hexdigest()
_SEG = 40_000  # → segments [0-39999], [40000-79999], [80000-102399]


def _range_response(request: httpx.Request) -> httpx.Response:
    rng = request.headers.get("Range")
    if rng and rng.startswith("bytes="):
        spec = rng.split("=", 1)[1]
        start_s, _, end_s = spec.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else len(_BLOB) - 1
        chunk = _BLOB[start : end + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(_BLOB)}",
                "Accept-Ranges": "bytes",
            },
        )
    return httpx.Response(200, content=_BLOB, headers={"Accept-Ranges": "bytes"})


# --------------------------------------------------------------------------- token


def test_read_hf_token_variants(tmp_path: Path):
    plain = tmp_path / "a.txt"
    plain.write_text("hf_ABCDEF123\n", encoding="utf-8")
    assert read_hf_token(plain) == "hf_ABCDEF123"

    with_comment = tmp_path / "b.txt"
    with_comment.write_text("# my token\n\n  HF_TOKEN=hf_XYZ789  \n", encoding="utf-8")
    assert read_hf_token(with_comment) == "hf_XYZ789"

    quoted = tmp_path / "c.txt"
    quoted.write_text('TOKEN="hf_QUOTED"\n', encoding="utf-8")
    assert read_hf_token(quoted) == "hf_QUOTED"

    assert read_hf_token(tmp_path / "missing.txt") is None


# ----------------------------------------------------------------- tree parsing


def test_parse_tree_entries_lfs_and_plain():
    entries = [
        {"type": "directory", "path": "d"},
        {
            "type": "file",
            "path": "model.safetensors",
            "size": 123,
            "lfs": {"oid": "a" * 64, "size": 5_000_000},
        },
        {"type": "file", "path": "config.json", "size": 42, "oid": "deadbeef"},
    ]
    files = parse_tree_entries(entries)
    assert len(files) == 2
    shard = next(f for f in files if f.path == "model.safetensors")
    assert shard.lfs is True and shard.size == 5_000_000 and shard.sha256 == "a" * 64
    cfg = next(f for f in files if f.path == "config.json")
    assert cfg.lfs is False and cfg.size == 42 and cfg.sha256 is None


def test_resolve_url():
    assert resolve_url("org/repo", "a/b.bin", revision="main") == (
        "https://huggingface.co/org/repo/resolve/main/a/b.bin"
    )


def test_plan_segments_covers_range_without_gaps():
    segs = _plan_segments(102_400, _SEG)
    assert segs == [(0, 39_999), (40_000, 79_999), (80_000, 102_399)]
    assert _plan_segments(0, _SEG) == []


# ---------------------------------------------------------------- verification


def test_verify_file_size_and_sha(tmp_path: Path):
    good = tmp_path / "g.bin"
    good.write_bytes(_BLOB)
    assert verify_file(good, size=len(_BLOB), sha256=_SHA) is True
    assert verify_file(good, size=len(_BLOB) + 1, sha256=_SHA) is False
    assert verify_file(good, size=len(_BLOB), sha256="b" * 64) is False
    assert verify_file(tmp_path / "nope.bin", size=1, sha256=None) is False


# ------------------------------------------------------------- file download


def test_download_file_segmented_and_verified(tmp_path: Path):
    client = httpx.Client(transport=httpx.MockTransport(_range_response))
    dest = tmp_path / "sub" / "model.bin"
    outcome = download_file(
        client, "https://hf/resolve/model.bin", dest,
        size=len(_BLOB), sha256=_SHA, segment_size=_SEG, segment_workers=3,
    )
    assert dest.exists() and dest.read_bytes() == _BLOB
    assert outcome.verified and not outcome.skipped
    # part + sidecar are cleaned up on success
    assert not dest.with_suffix(".bin.part").exists()
    assert not dest.with_suffix(".bin.part.json").exists()
    client.close()


def test_download_file_skips_when_already_valid(tmp_path: Path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _range_response(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "model.bin"
    dest.write_bytes(_BLOB)  # already complete + correct
    outcome = download_file(
        client, "https://hf/resolve/model.bin", dest, size=len(_BLOB), sha256=_SHA
    )
    assert outcome.skipped and outcome.verified
    assert seen == []  # nothing was fetched
    client.close()


def test_download_file_resumes_completed_segment(tmp_path: Path):
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("Range")
        if rng and rng != "bytes=0-0":
            ranges.append(rng)
        return _range_response(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "model.bin"
    part = dest.with_suffix(dest.suffix + ".part")
    # Pre-stage a run where segment 0 is already on disk, recorded in the sidecar.
    with part.open("wb") as handle:
        handle.truncate(len(_BLOB))
    with part.open("rb+") as handle:
        handle.seek(0)
        handle.write(_BLOB[0:_SEG])
    _segment_meta_path(part).write_text(
        json.dumps({"size": len(_BLOB), "segment_size": _SEG, "done": [0]}), encoding="utf-8"
    )

    outcome = download_file(
        client, "https://hf/resolve/model.bin", dest,
        size=len(_BLOB), sha256=_SHA, segment_size=_SEG, segment_workers=3,
    )
    assert dest.read_bytes() == _BLOB and outcome.verified
    # segment 0 was resumed (never re-requested); segments 1 and 2 were fetched
    assert "bytes=0-39999" not in ranges
    assert "bytes=40000-79999" in ranges and "bytes=80000-102399" in ranges
    assert outcome.resumed_from == _SEG
    client.close()


def test_download_file_sha_mismatch_raises(tmp_path: Path):
    client = httpx.Client(transport=httpx.MockTransport(_range_response))
    dest = tmp_path / "model.bin"
    with pytest.raises(ModelDownloadError, match="SHA256"):
        download_file(
            client, "https://hf/resolve/model.bin", dest,
            size=len(_BLOB), sha256="c" * 64, segment_size=_SEG,
        )
    assert not dest.exists()  # no partial masquerading as complete
    client.close()


# --------------------------------------------------------------- orchestration


def test_download_model_end_to_end(tmp_path: Path):
    files = [
        {
            "type": "file",
            "path": "model.bin",
            "size": len(_BLOB),
            "lfs": {"oid": _SHA, "size": len(_BLOB)},
        },
        {"type": "file", "path": "config.json", "size": len(_BLOB)},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/models/" in request.url.path:
            return httpx.Response(200, json=files)
        return _range_response(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    summary = download_model(
        "org/repo", tmp_path / "ckpt", token="hf_x", console=False, client=client,
        segment_size=_SEG, file_workers=2,
    )
    assert summary["files"] == 2 and summary["verified"] == 2
    assert (tmp_path / "ckpt" / "model.bin").read_bytes() == _BLOB
    assert (tmp_path / "ckpt" / "config.json").read_bytes() == _BLOB
    client.close()


def test_list_repo_files_auth_error():
    from jarvis_gpt.model_downloader import list_repo_files

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(ModelDownloadError, match="401"):
        list_repo_files("org/repo", client=client)
    client.close()


def test_remote_file_defaults():
    rf = RemoteFile(path="x", size=1)
    assert rf.sha256 is None and rf.lfs is False
