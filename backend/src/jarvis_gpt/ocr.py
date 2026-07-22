from __future__ import annotations

import asyncio
import base64
import io
import math
import mimetypes
from contextlib import suppress
from pathlib import Path
from typing import Any

OCR_MAX_PAGES = 30
OCR_RASTER_SCALE = 2.0
OCR_MAX_IMAGE_BYTES = 16 * 1024 * 1024
OCR_MAX_PAGE_PIXELS = 8_000_000
OCR_MAX_PAGE_DIMENSION = 8_192
OCR_MAX_PAGE_PNG_BYTES = 16 * 1024 * 1024
OCR_IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
OCR_PROMPT = (
    "You are an exact multilingual OCR engine. Transcribe every visible character "
    "verbatim, preserving line order and punctuation. Never translate, summarize, "
    "explain, or invent text. Return only the transcription. If no readable text is "
    "present, return exactly: [no text]."
)


def _open_pdf_page_count(path: Path) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError("pypdfium2 is required for scanned PDF OCR") from exc

    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


def _bounded_pdf_scale(
    width: float,
    height: float,
    requested_scale: float,
    *,
    max_pixels: int = OCR_MAX_PAGE_PIXELS,
    max_dimension: int = OCR_MAX_PAGE_DIMENSION,
) -> float:
    """Return a render scale that bounds both pixels and either page dimension."""

    width = max(1.0, float(width))
    height = max(1.0, float(height))
    minimum_scale = max(1.0 / width, 1.0 / height)
    scale = max(minimum_scale, float(requested_scale))
    scale = min(
        scale,
        math.sqrt(max(1, int(max_pixels)) / (width * height)),
        max(1, int(max_dimension)) / width,
        max(1, int(max_dimension)) / height,
    )
    return max(minimum_scale, scale)


def _rasterize_pdf_page(
    path: Path,
    page_index: int,
    *,
    scale: float,
    max_pixels: int = OCR_MAX_PAGE_PIXELS,
    max_dimension: int = OCR_MAX_PAGE_DIMENSION,
    max_png_bytes: int = OCR_MAX_PAGE_PNG_BYTES,
) -> tuple[bytes, dict[str, Any]]:
    """Render exactly one PDF page under strict pixel and encoded-byte limits."""

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError("pypdfium2 is required for scanned PDF OCR") from exc

    document = pdfium.PdfDocument(str(path))
    page = None
    bitmap = None
    image = None
    resized = None
    try:
        if page_index < 0 or page_index >= len(document):
            raise IndexError(f"PDF page index is out of range: {page_index}")
        page = document[page_index]
        width, height = page.get_size()
        bounded_scale = _bounded_pdf_scale(
            width,
            height,
            scale,
            max_pixels=max_pixels,
            max_dimension=max_dimension,
        )
        bitmap = page.render(scale=bounded_scale)
        image = bitmap.to_pil()

        current = image
        encoded = b""
        for _attempt in range(4):
            buffer = io.BytesIO()
            current.save(buffer, format="PNG", optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= max_png_bytes:
                break
            ratio = math.sqrt(max_png_bytes / max(1, len(encoded))) * 0.9
            new_size = (
                max(1, int(current.width * ratio)),
                max(1, int(current.height * ratio)),
            )
            if new_size == current.size:
                break
            if resized is not None:
                resized.close()
            resized = image.resize(new_size)
            current = resized
        if len(encoded) > max_png_bytes:
            raise ValueError(
                f"rendered PDF page exceeds the {max_png_bytes}-byte OCR limit"
            )
        return encoded, {
            "width": int(current.width),
            "height": int(current.height),
            "pixels": int(current.width * current.height),
            "png_bytes": len(encoded),
            "requested_scale": float(scale),
            "render_scale": round(float(bounded_scale), 6),
            "scale_reduced": bounded_scale < float(scale),
        }
    finally:
        if resized is not None:
            resized.close()
        if image is not None:
            image.close()
        if bitmap is not None:
            with suppress(Exception):
                bitmap.close()
        if page is not None:
            with suppress(Exception):
                page.close()
        document.close()


async def _recognize_image(llm: Any, image_bytes: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    result = await llm.complete(
        [
            {"role": "system", "content": OCR_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Transcribe all visible text in its original language.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                    },
                ],
            },
        ],
        thinking_enabled=False,
    )
    if not getattr(result, "ok", False):
        raise RuntimeError("the configured vision model rejected the OCR request")
    text = str(getattr(result, "content", "") or "").strip()
    if text.casefold() in {"[no text]", "[нет текста]"}:
        return ""
    return text


async def extract_ocr_job(
    job: dict[str, Any],
    llm: Any,
    *,
    profile_name: str,
) -> dict[str, Any]:
    """Extract one leased OCR job without mutating persistence state."""

    path = Path(str(job.get("stored_path") or "")).resolve(strict=False)
    if not path.is_file():
        raise FileNotFoundError("OCR source file no longer exists")
    suffix = path.suffix.casefold()
    mime_type = str(job.get("mime_type") or "").strip().casefold()
    if not mime_type:
        mime_type = (mimetypes.guess_type(path.name)[0] or "").casefold()

    source_page_count = 1
    pages_attempted = 1
    pages_rasterized = 0
    page_render_details: list[dict[str, Any]] = []
    recognized: list[str] = []
    failed_labels: list[str] = []
    if suffix == ".pdf" or mime_type == "application/pdf":
        source_page_count = await asyncio.to_thread(_open_pdf_page_count, path)
        pages_attempted = min(source_page_count, OCR_MAX_PAGES)
        if pages_attempted <= 0:
            raise ValueError("OCR source contains no renderable pages")
        for index in range(pages_attempted):
            label = f"page {index + 1}"
            try:
                image, render_details = await asyncio.to_thread(
                    _rasterize_pdf_page,
                    path,
                    index,
                    scale=OCR_RASTER_SCALE,
                )
                pages_rasterized += 1
                page_render_details.append({"page": index + 1, **render_details})
                text = await _recognize_image(llm, image, "image/png")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - partial PDFs retain successful pages.
                failed_labels.append(label)
                continue
            finally:
                image = b""
            if text:
                recognized.append(
                    f"--- {label} ---\n{text}" if pages_attempted > 1 else text
                )
    elif mime_type.startswith("image/") or suffix in OCR_IMAGE_EXTENSIONS:
        actual_size = path.stat().st_size
        if actual_size > OCR_MAX_IMAGE_BYTES:
            raise ValueError("OCR image exceeds the 16 MiB safety limit")
        image = await asyncio.to_thread(path.read_bytes)
        pages_rasterized = 1
        try:
            text = await _recognize_image(llm, image, mime_type or "image/png")
        finally:
            image = b""
        if text:
            recognized.append(text)
    else:
        raise ValueError("automatic OCR supports images and scanned PDFs only")
    transcript = "\n\n".join(recognized).strip()
    if not transcript:
        raise RuntimeError("the vision model did not recognize text in any page")
    indexed_text = transcript[:200_000]
    warnings: list[str] = []
    if source_page_count > pages_attempted:
        warnings.append(
            f"OCR attempted the first {pages_attempted} of {source_page_count} page(s)."
        )
    if failed_labels:
        warnings.append(f"OCR failed for {len(failed_labels)} of {pages_attempted} part(s).")
    if len(indexed_text) < len(transcript):
        warnings.append(
            f"OCR indexed the first {len(indexed_text)} of {len(transcript)} character(s)."
        )
    return {
        "text": indexed_text,
        "source": f"vlm_ocr:{profile_name}",
        "details": {
            "pages_total": source_page_count,
            "pages_attempted": pages_attempted,
            "pages_rasterized": pages_rasterized,
            "pages_recognized": len(recognized),
            "pages_failed": len(failed_labels),
            "pages_truncated": max(0, source_page_count - pages_attempted),
            "page_render_details": page_render_details,
            "page_limits": {
                "max_pages": OCR_MAX_PAGES,
                "max_pixels": OCR_MAX_PAGE_PIXELS,
                "max_dimension": OCR_MAX_PAGE_DIMENSION,
                "max_png_bytes": OCR_MAX_PAGE_PNG_BYTES,
            },
            "characters_recognized": len(transcript),
            "characters_indexed": len(indexed_text),
            "text_truncated": len(indexed_text) < len(transcript),
            "automatic": True,
        },
        "warning": " ".join(warnings) or None,
    }
