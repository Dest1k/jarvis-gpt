from __future__ import annotations

import hashlib
import json
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1] / "evidence" / "gui-fixtures"
MANIFEST = ROOT / "profile-pdf-fixtures-manifest.json"


def exclusive_text(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def build_pdf(path: Path, repeat: int) -> None:
    if path.exists():
        raise FileExistsError(path)
    drawing = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER
    drawing.setTitle(f"Mono profile recovery fixture {repeat}")
    drawing.setFont("Helvetica-Bold", 16)
    drawing.drawString(72, height - 72, "Recovery verification report")
    drawing.setFont("Helvetica", 11)
    lines = [
        f"fixture: mono-perf-good-{repeat}.pdf",
        f"checksum_marker: MPG-{repeat}-A7C9",
        "conclusion: controlled recovery completed successfully",
    ]
    y = height - 108
    for line in lines:
        if stringWidth(line, "Helvetica", 11) > width - 144:
            raise ValueError(f"line too wide: {line}")
        drawing.drawString(72, y, line)
        y -= 22
    drawing.setFont("Helvetica-Oblique", 9)
    drawing.drawString(72, 54, f"Synthetic functional fixture {repeat} - page 1 of 1")
    drawing.showPage()
    drawing.save()


def main() -> int:
    if MANIFEST.exists():
        raise SystemExit(f"refusing to overwrite {MANIFEST}")
    created: list[Path] = []
    for repeat in range(1, 4):
        good = ROOT / f"mono-perf-good-{repeat}.pdf"
        build_pdf(good, repeat)
        created.append(good)
        bad = ROOT / f"mono-perf-bad-{repeat}.pdf"
        with bad.open("xb") as handle:
            handle.write(b"%PDF-1.7\n% intentionally truncated functional fixture\n")
        created.append(bad)
        left = ROOT / f"mono-mission-a-{repeat}.txt"
        exclusive_text(left, f"version={repeat}.0\nowner=alpha-team\n")
        created.append(left)
        right = ROOT / f"mono-mission-b-{repeat}.txt"
        exclusive_text(right, f"version={repeat}.1\nowner=beta-team\n")
        created.append(right)
    for repeat in range(1, 3):
        doc = ROOT / f"mono-perf-doc-{repeat}.txt"
        exclusive_text(doc, f"checksum_marker=MP-DOC-{repeat}-4E2B\nstate=ready\n")
        created.append(doc)
    payload = {
        "schema": "jarvis.functional-profile-pdf-fixtures.v1",
        "count": len(created),
        "files": [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(created)
        ],
    }
    with MANIFEST.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"manifest": str(MANIFEST), "count": len(created)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
