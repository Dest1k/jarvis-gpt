#!/usr/bin/env python3
"""Repository-local launcher for the Jarvis CLI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jarvis_gpt.cli import main


if __name__ == "__main__":
    main()
