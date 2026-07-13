#!/usr/bin/env python3
"""Create a non-overwriting redacted copy of textual JSON/log evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


PATTERNS = (
    re.compile(r"(?i)(JARVIS_API_TOKEN\s*[:=]\s*)([^\s,\"'\\]+)"),
    re.compile(r"(?i)(api[_-]?token[\"']?\s*:\s*[\"'])([^\"']+)([\"'])"),
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)([^\s,\"'\\]+)"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    text = args.input.read_text(encoding="utf-8")
    replacements = 0
    for pattern in PATTERNS:
        if pattern.groups == 3:
            text, count = pattern.subn(r"\1<redacted>\3", text)
        else:
            text, count = pattern.subn(r"\1<redacted>", text)
        replacements += count
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"redactions={replacements} output={args.output}")
    return 0 if replacements else 2


if __name__ == "__main__":
    raise SystemExit(main())
