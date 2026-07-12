"""Optional vision helpers (OCR when host tools are available).

Experimental ideal-branch module. Production document OCR readiness is reported
by ``document_surfer`` / ``documents.analyze`` without requiring this module.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("jarvis.vision")

MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass
class VisionAnalysis:
    description: str
    key_entities: list[str] = field(default_factory=list)
    ocr_text: str | None = None
    safety_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source_type: str = "uploaded_image"
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "key_entities": list(self.key_entities),
            "ocr_text": self.ocr_text,
            "safety_flags": list(self.safety_flags),
            "confidence": self.confidence,
            "source_type": self.source_type,
            "source_path": self.source_path,
        }


class VisionManager:
    def analyze_image(
        self,
        image_path: str | Path,
        query: str | None = None,
        source_type: str = "uploaded_image",
    ) -> VisionAnalysis:
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"Image too large ({size} > {MAX_IMAGE_BYTES} bytes)")

        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        description = f"{path.name} ({digest})"
        if query:
            description += f" | {query}"

        ocr_text: str | None = None
        confidence = 0.35
        safety_flags = ["path_checked", "size_checked"]
        try:
            from PIL import Image  # type: ignore[import-not-found]
            import pytesseract  # type: ignore[import-not-found]

            ocr_text = pytesseract.image_to_string(Image.open(path), lang="rus+eng")
            confidence = 0.8 if (ocr_text or "").strip() else 0.45
            safety_flags.append("ocr_attempted")
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("OCR unavailable for %s: %s", path, exc)
            safety_flags.append("ocr_unavailable")

        return VisionAnalysis(
            description=description,
            key_entities=["text", "ui"] if "screenshot" in source_type else [],
            ocr_text=ocr_text,
            safety_flags=safety_flags,
            confidence=confidence,
            source_type=source_type,
            source_path=str(path),
        )

    def analyze_screenshot(
        self,
        path: str | Path,
        query: str | None = None,
    ) -> VisionAnalysis:
        return self.analyze_image(path, query, "screenshot")

    def analyze_pdf_page(
        self,
        path: str | Path,
        page_number: int = 1,
        query: str | None = None,
    ) -> VisionAnalysis:
        # Page rasterization requires pdftoppm; fall back to metadata-only analysis.
        return self.analyze_image(
            path,
            query or f"Page {page_number}",
            "pdf_page",
        )


def get_vision_tools() -> dict[str, Any]:
    manager = VisionManager()
    return {
        "vision.analyze": manager.analyze_image,
        "vision.screenshot": manager.analyze_screenshot,
        "vision.pdf_page": manager.analyze_pdf_page,
    }
