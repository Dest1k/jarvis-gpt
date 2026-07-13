#!/usr/bin/env python3
"""Hermetic reproductions whose non-zero exit means the safety oracle failed."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUN_ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))


def collision() -> int:
    import jarvis_gpt.document_surfer as module
    from jarvis_gpt.document_surfer import DocumentSurferConfig, JarvisDocumentSurfer

    class FixedDatetime:
        @classmethod
        def now(cls, _tz: object) -> datetime:
            return datetime(2026, 7, 13, 1, 2, 3, tzinfo=UTC)

    with tempfile.TemporaryDirectory(prefix="jarvis-audit-collision-") as temp:
        root = Path(temp)
        source = root / "source.txt"
        source.write_text("source", encoding="utf-8")
        surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=root / "out"))
        original_datetime = module.datetime
        module.datetime = FixedDatetime
        try:
            first = surfer._resolve_output_path(
                source,
                output_path=None,
                output_name="same.md",
                default_suffix=".md",
                stem_suffix="-generated",
            )
            first.write_text("first", encoding="utf-8")
            second = surfer._resolve_output_path(
                source,
                output_path=None,
                output_name="same.md",
                default_suffix=".md",
                stem_suffix="-generated",
            )
            second.write_text("second", encoding="utf-8")
            third = surfer._resolve_output_path(
                source,
                output_path=None,
                output_name="same.md",
                default_suffix=".md",
                stem_suffix="-generated",
            )
        finally:
            module.datetime = original_datetime
        observed = {
            "first": first.name,
            "second": second.name,
            "third": third.name,
            "third_collides_with_existing_second": third == second and third.exists(),
            "oracle": "every implicit output path is unique and never overwrites an existing output",
        }
        print(json.dumps(observed, indent=2))
        return 1 if observed["third_collides_with_existing_second"] else 0


def archive_partial() -> int:
    from jarvis_gpt.archive_runtime import ArchiveConfig, ArchiveSafetyError, extract_archive

    with tempfile.TemporaryDirectory(prefix="jarvis-audit-archive-") as temp:
        root = Path(temp)
        zip_path = root / "sample.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("first.txt", b"1234")
            archive.writestr("second.txt", b"5678")
        zip_out = root / "zip-out"
        zip_error = None
        try:
            extract_archive(
                zip_path,
                output_dir=zip_out,
                config=ArchiveConfig(max_member_bytes=10, max_total_uncompressed_bytes=5),
            )
        except ArchiveSafetyError as exc:
            zip_error = str(exc)

        gzip_path = root / "payload.txt.gz"
        with gzip.open(gzip_path, "wb") as handle:
            handle.write(b"payload")
        gzip_out = root / "gzip-out"
        gzip_error = None
        try:
            extract_archive(
                gzip_path,
                output_dir=gzip_out,
                config=ArchiveConfig(max_member_bytes=2, max_total_uncompressed_bytes=20),
            )
        except ArchiveSafetyError as exc:
            gzip_error = str(exc)

        zip_files = {p.name: p.stat().st_size for p in zip_out.rglob("*") if p.is_file()}
        gzip_files = {p.name: p.stat().st_size for p in gzip_out.rglob("*") if p.is_file()}
        observed = {
            "zip_error": zip_error,
            "zip_partial_files_after_failure": zip_files,
            "gzip_error": gzip_error,
            "gzip_partial_files_after_failure": gzip_files,
            "oracle": "failed extraction leaves no final output files",
        }
        print(json.dumps(observed, indent=2))
        violated = bool(zip_error and zip_files) or bool(gzip_error and gzip_files)
        return 1 if violated else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=["collision", "archive-partial"])
    args = parser.parse_args()
    if args.case == "collision":
        return collision()
    return archive_partial()


if __name__ == "__main__":
    raise SystemExit(main())
