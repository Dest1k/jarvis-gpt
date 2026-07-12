from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.model_catalog import MODEL_OVERRIDE_KEY
from jarvis_gpt.model_hub import (
    DOWNLOAD_JOBS_KEY,
    DownloadedFile,
    ModelHubManager,
    _download_file,
    _safe_model_file_path,
)
from jarvis_gpt.storage import JarvisStorage


@pytest.mark.parametrize(
    ("profile_name", "model_id"),
    [
        ("gemma4-mono", "gemma4-26b-a4b-nvfp4"),
        ("gemma4-mono-perf", "gemma4-26b-a4b-nvfp4"),
        ("gemma4-turbo", "gemma4-31b-it-nvfp4"),
    ],
)
def test_model_hub_rejects_cross_profile_builtin_activation(
    monkeypatch,
    tmp_path,
    profile_name,
    model_id,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    settings = load_settings(profile_name)
    ensure_runtime_dirs(settings)
    (settings.model_root / model_id).mkdir(parents=True)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    manager = ModelHubManager(settings=settings, storage=storage)

    with pytest.raises(ValueError, match="belongs to another built-in profile"):
        manager.activate_model(model_id)

    assert storage.get_runtime_value(MODEL_OVERRIDE_KEY, None) is None
    storage.close()


def test_model_hub_keeps_custom_model_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    settings = load_settings("gemma4-mono")
    ensure_runtime_dirs(settings)
    custom = settings.model_root / "owner__custom-7b-q4"
    custom.mkdir(parents=True)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    manager = ModelHubManager(settings=settings, storage=storage)

    result = manager.activate_model(custom.name)

    assert result["model_id"] == custom.name
    assert storage.get_runtime_value(MODEL_OVERRIDE_KEY, None) == custom.name
    storage.close()


def test_download_file_resumes_part_with_range(monkeypatch, tmp_path):
    target_root = tmp_path / "models"
    target_root.mkdir()
    partial = target_root / "model.safetensors.part"
    partial.write_bytes(b"abc")
    captured = {}

    class FakeResponse:
        status = 206

        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def getcode(self) -> int:
            return self.status

        def read(self, size: int = -1) -> bytes:
            if self.offset >= len(self.payload):
                return b""
            if size < 0:
                size = len(self.payload) - self.offset
            chunk = self.payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    def fake_urlopen(request, timeout=60):
        captured["range"] = request.get_header("Range")
        return FakeResponse(b"def")

    monkeypatch.setattr("jarvis_gpt.model_hub.urllib.request.urlopen", fake_urlopen)

    result = _download_file(
        repo_id="owner/model",
        revision="main",
        relative_path="model.safetensors",
        target_root=target_root,
        token="token",
        expected_size=6,
    )

    assert captured["range"] == "bytes=3-"
    assert (target_root / "model.safetensors").read_bytes() == b"abcdef"
    assert not partial.exists()
    assert result.resumed_from == 3
    assert result.size == 6


def test_download_file_discards_oversized_partial(monkeypatch, tmp_path):
    target_root = tmp_path / "models"
    target_root.mkdir()
    partial = target_root / "model.safetensors.part"
    partial.write_bytes(b"corrupt")
    captured = {}

    class FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.payload = b"abcdef"
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def getcode(self) -> int:
            return self.status

        def read(self, size: int = -1) -> bytes:
            if self.offset:
                return b""
            self.offset = len(self.payload)
            return self.payload

    def fake_urlopen(request, timeout=60):
        captured["range"] = request.get_header("Range")
        return FakeResponse()

    monkeypatch.setattr("jarvis_gpt.model_hub.urllib.request.urlopen", fake_urlopen)

    result = _download_file(
        repo_id="owner/model",
        revision="main",
        relative_path="model.safetensors",
        target_root=target_root,
        token="",
        expected_size=6,
    )

    assert captured["range"] is None
    assert (target_root / "model.safetensors").read_bytes() == b"abcdef"
    assert result.skipped is False


def test_model_download_worker_uses_parallel_file_workers(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    manager = ModelHubManager(settings=settings, storage=storage)
    files = [
        {"rfilename": "a.safetensors", "size": 1},
        {"rfilename": "b.safetensors", "size": 1},
        {"rfilename": "tokenizer.json", "size": 1},
    ]
    job = {
        "id": "job_parallel",
        "repo_id": "owner/model",
        "revision": "main",
        "status": "queued",
        "summary": "",
        "target": str(settings.model_root / "owner__model"),
        "total_files": len(files),
        "completed_files": 0,
        "total_bytes": 3,
        "downloaded_bytes": 0,
        "current_file": "",
        "error": "",
        "workers": 3,
        "resumable": True,
        "created_at": "now",
        "updated_at": "now",
    }
    storage.set_runtime_value(DOWNLOAD_JOBS_KEY, [job])
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_download(
        *,
        repo_id: str,
        revision: str,
        relative_path: str,
        target_root: Path,
        token: str,
        expected_size: int,
        part_workers: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> DownloadedFile:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        target = target_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        with lock:
            active -= 1
        return DownloadedFile(relative_path=relative_path, size=1, resumed_from=0)

    monkeypatch.setattr("jarvis_gpt.model_hub._download_file", fake_download)

    manager._download_worker("job_parallel", "owner/model", "main", files, workers=3)
    updated = manager.download_jobs()[0]

    assert max_active > 1
    assert updated["status"] == "done"
    assert updated["completed_files"] == 3
    assert updated["downloaded_bytes"] == 3
    storage.close()


def test_download_file_uses_parallel_segments(monkeypatch, tmp_path):
    target_root = tmp_path / "models"
    target_root.mkdir()
    payload = b"abcdef"
    requested_ranges = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    class FakeResponse:
        status = 206

        def __init__(self, body: bytes, delay: bool = False) -> None:
            self.body = body
            self.offset = 0
            self.delay = delay

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def getcode(self) -> int:
            return self.status

        def read(self, size: int = -1) -> bytes:
            if self.delay and self.offset == 0:
                time.sleep(0.05)
            if self.offset >= len(self.body):
                return b""
            if size < 0:
                size = len(self.body) - self.offset
            chunk = self.body[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    def fake_urlopen(request, timeout=60):
        nonlocal active, max_active
        range_header = request.get_header("Range")
        assert range_header is not None
        if range_header == "bytes=0-0":
            return FakeResponse(payload[:1])
        requested_ranges.append(range_header)
        match = range_header.removeprefix("bytes=").split("-")
        start = int(match[0])
        end = int(match[1])
        with lock:
            active += 1
            max_active = max(max_active, active)
        body = payload[start : end + 1]

        class TrackingResponse(FakeResponse):
            def __exit__(self, exc_type, exc, tb):
                nonlocal active
                with lock:
                    active -= 1
                return False

        return TrackingResponse(body, delay=True)

    monkeypatch.setattr("jarvis_gpt.model_hub.SEGMENT_DOWNLOAD_MIN_BYTES", 1)
    monkeypatch.setattr("jarvis_gpt.model_hub.urllib.request.urlopen", fake_urlopen)

    result = _download_file(
        repo_id="owner/model",
        revision="main",
        relative_path="model.safetensors",
        target_root=target_root,
        token="token",
        expected_size=len(payload),
        part_workers=3,
    )

    assert sorted(requested_ranges) == ["bytes=0-1", "bytes=2-3", "bytes=4-5"]
    assert max_active > 1
    assert (target_root / "model.safetensors").read_bytes() == payload
    assert not (target_root / "model.safetensors.segments").exists()
    assert result.size == len(payload)


@pytest.mark.parametrize(
    "relative_path",
    ["../escape.json", "nested/../../escape.json", "C:/escape.json"],
)
def test_model_file_path_cannot_escape_download_root(tmp_path, relative_path):
    with pytest.raises(ValueError, match="Unsafe model file path"):
        _safe_model_file_path(tmp_path / "models", relative_path)


def test_model_hub_recovers_interrupted_jobs_on_start(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value(
        DOWNLOAD_JOBS_KEY,
        [
            {
                "id": "stale",
                "repo_id": "owner/model",
                "target": str(settings.model_root / "owner__model"),
                "status": "running",
            }
        ],
    )

    manager = ModelHubManager(settings=settings, storage=storage)

    recovered = manager.download_jobs()[0]
    assert recovered["status"] == "error"
    assert "restart to resume" in recovered["summary"]
    storage.close()


def test_model_hub_rejects_duplicate_target_download(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    manager = ModelHubManager(settings=settings, storage=storage)
    storage.set_runtime_value(
        DOWNLOAD_JOBS_KEY,
        [
            {
                "id": "active",
                "repo_id": "owner/model",
                "target": str(settings.model_root / "owner__model"),
                "status": "running",
            }
        ],
    )
    monkeypatch.setattr(
        manager,
        "_model_info",
        lambda _repo_id: {
            "siblings": [{"rfilename": "config.json", "size": 2}],
        },
    )

    with pytest.raises(ValueError, match="already active"):
        manager.start_download("owner/model")

    storage.close()


def test_model_download_worker_honors_preexisting_cancel(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    manager = ModelHubManager(settings=settings, storage=storage)
    job = {
        "id": "cancelled-job",
        "repo_id": "owner/model",
        "target": str(settings.model_root / "owner__model"),
        "status": "queued",
    }
    storage.set_runtime_value(DOWNLOAD_JOBS_KEY, [job])
    cancel_event = threading.Event()
    cancel_event.set()

    manager._download_worker(
        job["id"],
        "owner/model",
        "main",
        [{"rfilename": "config.json", "size": 2}],
        workers=1,
        cancel_event=cancel_event,
    )

    updated = manager.download_jobs()[0]
    assert updated["status"] == "cancelled"
    assert updated["current_file"] == ""
    storage.close()


def test_shutdown_worker_registry_blocks_overlapping_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    manager = ModelHubManager(settings=settings, storage=storage)
    entered = threading.Event()
    release = threading.Event()
    info = {"siblings": [{"rfilename": "config.json", "size": 1}]}
    monkeypatch.setattr(manager, "_model_info", lambda _repo_id: info)

    def blocked_download(
        *,
        repo_id: str,
        revision: str,
        relative_path: str,
        target_root: Path,
        token: str,
        expected_size: int,
        part_workers: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> DownloadedFile:
        entered.set()
        release.wait(timeout=2)
        target = target_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return DownloadedFile(relative_path=relative_path, size=1, resumed_from=0)

    monkeypatch.setattr("jarvis_gpt.model_hub._download_file", blocked_download)
    job = manager.start_download("owner/model")
    assert entered.wait(timeout=1)
    worker = manager._workers[job["id"]]

    manager.close(timeout=0)
    replacement = ModelHubManager(settings=settings, storage=storage)
    monkeypatch.setattr(replacement, "_model_info", lambda _repo_id: info)

    with pytest.raises(ValueError, match="still active|already active"):
        replacement.start_download("owner/model")

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    replacement.close()
    storage.close()
