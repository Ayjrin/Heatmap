#!/usr/bin/env python3
"""Compatibility entrypoint for the manual compact ETL collector."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from proleague.pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
