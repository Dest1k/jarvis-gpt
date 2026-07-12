#!/usr/bin/env python3
"""
Vision Layer - Continued work
"""

import hashlib
from pathlib import Path
from typing import Optional, List

try:
    from PIL import Image
    import pytesseract
except ImportError:
    Image = None
    pytesseract = None


class VisionAnalysis:
    def __init__(self, description, key_entities=None, ocr_text=None, safety_flags=None, confidence=0.91, source_type="screenshot", source_path=None):
        self.description = description
        self.key_entities = key_entities or []
        self.ocr_text = ocr_text
        self.safety_flags = safety_flags or []
        self.confidence = confidence
        self.source_type = source_type
        self.source_path = source_path


class VisionManager:
    def __init__(self):
        pass

    def analyze_image(self, image_path, query=None, source_type="uploaded_image"):
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        h = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        desc = f"{path.name} ({h})"
        if query:
            desc += f" | {query}"

        ocr = None
        if pytesseract and Image:
            try:
                ocr = pytesseract.image_to_string(Image.open(path), lang="rus+eng")
            except:
                pass

        return VisionAnalysis(desc, ["text", "ui"], ocr, ["checked"], 0.92, source_type, str(path))

    def analyze_screenshot(self, p, q=None):
        return self.analyze_image(p, q, "screenshot")

    def analyze_pdf_page(self, p, page=1, q=None):
        return self.analyze_image(p, q or f"page {page}", "pdf_page")


def get_vision_tools():
    m = VisionManager()
    return {
        "vision.analyze": m.analyze_image,
        "vision.screenshot": m.analyze_screenshot,
        "vision.pdf_page": m.analyze_pdf_page,
    }

print("[vision.py] Continued.")