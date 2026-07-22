from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import jarvis_gpt.ocr as ocr
from jarvis_gpt.authorization import ActorContext, AuthorizationService, bind_actor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor


class _VisionLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, **_kwargs):
        self.calls += 1
        assert messages[1]["content"][1]["type"] == "image_url"
        return SimpleNamespace(
            ok=True,
            content=(
                "Project Atlas invoice CN-42. "
                "发票编号 ZH-42. 청구서 번호 KR-42. 請求書番号 JP-42."
            ),
        )


def test_pdf_ocr_reports_page_cap_without_claiming_full_index(monkeypatch, tmp_path):
    source = tmp_path / "large-scan.pdf"
    source.write_bytes(b"%PDF synthetic")
    monkeypatch.setattr(
        ocr,
        "_open_pdf_page_count",
        lambda *_args, **_kwargs: 41,
    )
    monkeypatch.setattr(
        ocr,
        "_rasterize_pdf_page",
        lambda *_args, **_kwargs: (
            b"page image",
            {"width": 10, "height": 10, "pixels": 100, "png_bytes": 10},
        ),
    )

    result = asyncio.run(
        ocr.extract_ocr_job(
            {
                "stored_path": str(source),
                "mime_type": "application/pdf",
                "file_size": source.stat().st_size,
            },
            _VisionLLM(),
            profile_name="qwen36-vl",
        )
    )

    assert result["details"]["pages_total"] == 41
    assert result["details"]["pages_attempted"] == ocr.OCR_MAX_PAGES
    assert result["details"]["pages_truncated"] == 11
    assert "first 30 of 41 page(s)" in result["warning"]


def test_pdf_ocr_rasterizes_and_recognizes_one_page_at_a_time(monkeypatch, tmp_path):
    source = tmp_path / "sequential.pdf"
    source.write_bytes(b"%PDF synthetic")
    events: list[str] = []
    monkeypatch.setattr(ocr, "_open_pdf_page_count", lambda *_args: 3)

    def rasterize(_path, index, **_kwargs):
        events.append(f"raster:{index}")
        return bytes([index]), {
            "width": 10,
            "height": 10,
            "pixels": 100,
            "png_bytes": 1,
        }

    async def recognize(_llm, image, _mime):
        index = int(image[0])
        events.append(f"recognize:{index}")
        return f"page text {index}"

    monkeypatch.setattr(ocr, "_rasterize_pdf_page", rasterize)
    monkeypatch.setattr(ocr, "_recognize_image", recognize)

    result = asyncio.run(
        ocr.extract_ocr_job(
            {"stored_path": str(source), "mime_type": "application/pdf"},
            object(),
            profile_name="qwen36-vl",
        )
    )

    assert events == [
        "raster:0",
        "recognize:0",
        "raster:1",
        "recognize:1",
        "raster:2",
        "recognize:2",
    ]
    assert result["details"]["pages_rasterized"] == 3
    assert result["details"]["pages_recognized"] == 3


def test_pdf_render_scale_is_bounded_by_pixels_and_dimensions():
    scale = ocr._bounded_pdf_scale(
        20_000,
        10_000,
        4.0,
        max_pixels=1_000_000,
        max_dimension=2_000,
    )

    assert 20_000 * scale <= 2_000
    assert 10_000 * scale <= 2_000
    assert (20_000 * scale) * (10_000 * scale) <= 1_000_000.001


def test_supervisor_drains_durable_ocr_queue_for_ordinary_tenant(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    identity = AuthorizationService(storage).upsert_external_identity(
        provider="test",
        realm_id="ocr-supervisor",
        provider_subject_id="ordinary",
        bootstrap_preset="user",
    )
    actor = ActorContext(
        user_id=str(identity["user_id"]),
        preset_key=str(identity["preset_key"]),
        source="test",
        identity_id=str(identity["identity_id"]),
        policy_epoch=int(identity["policy_epoch"]),
    )
    with bind_actor(actor):
        uploaded = FileIngestor(settings, storage).ingest_upload(
            "telegram-scan.png",
            io.BytesIO(b"synthetic image bytes forwarded to fake VLM"),
        )
        assert uploaded["ocr_job"]["status"] == "pending"

    llm = _VisionLLM()
    supervisor = RuntimeSupervisor(settings=settings, storage=storage, llm=llm)
    assert asyncio.run(supervisor._run_ocr_job()) is True
    assert asyncio.run(supervisor._run_ocr_job()) is False
    assert llm.calls == 1
    assert supervisor.status()["last_ocr_error"] is None

    with bind_actor(actor):
        job = storage.get_file_ocr_job_for_file(str(uploaded["file"]["id"]))
        assert job is not None
        assert job["status"] == "completed"
        assert storage.search_file_chunks("Project Atlas", limit=5)
        assert storage.search_file_chunks("发票编号", limit=5)
        assert storage.search_file_chunks("청구서 번호", limit=5)
        assert storage.search_file_chunks("請求書番号", limit=5)
        metadata = storage.get_file_index_metadata(str(uploaded["file"]["id"]))
        assert metadata["source"] == "vlm_ocr:qwen36-vl"
        assert metadata["details"]["automatic"] is True

    storage.close()
