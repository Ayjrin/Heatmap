"""Bronze store: immutable, gzipped raw JSON on disk.

RIOT_LOL_API.md §8: "Match data is immutable once a game ends, so cache it
permanently." Bronze is therefore write-once and never re-fetched; every
downstream reprocess runs offline against these files, so the rate limit is
paid exactly once per match.
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any


def write_json_gz(path: Path, payload: Any) -> Path:
    """Atomic gzipped write -- a killed process must not leave a half file that
    later reads as a cache hit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def read_json_gz(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return path


def exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def iter_gz(directory: Path):
    """Yield (stem, payload) for every .json.gz in a bronze directory."""
    if not directory.exists():
        return
    for p in sorted(directory.glob("*.json.gz")):
        yield p.name.removesuffix(".json.gz"), read_json_gz(p)
