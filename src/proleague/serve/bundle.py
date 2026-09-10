"""Struct-of-typed-arrays wire format for the browser.

PROJECT_PLAN §4.2 calls this `core.arrow` / `extended.arrow`. It is deliberately
NOT Arrow IPC, and the reason is worth stating: every column here is a
fixed-width scalar with a sentinel null (§4.2), there are no strings, no
validity bitmaps and no nesting. Arrow's file format exists to carry exactly the
things we do not have, and Arrow-JS costs ~150 KB gzipped to parse a layout we
can consume with zero copies:

    const col = new Int16Array(buffer, offset, rowCount);

So the format is: one `.bin` of concatenated column buffers, each 8-byte
aligned, plus a JSON manifest of (name, type, byteOffset). Parse cost in the
browser is zero -- the typed arrays are views onto the fetched ArrayBuffer.

Columns are split across two files so first paint does not wait on the full
payload; see CORE_COLUMNS in transform/kills.py.
"""
from __future__ import annotations

import array
import json
import sys
from pathlib import Path

from ..transform.kills import COLUMNS, CORE_COLUMNS

# type code -> (python array typecode, bytes, JS TypedArray name)
TYPES = {
    "u8":  ("B", 1, "Uint8Array"),
    "i8":  ("b", 1, "Int8Array"),
    "u16": ("H", 2, "Uint16Array"),
    "i16": ("h", 2, "Int16Array"),
    "u32": ("I", 4, "Uint32Array"),
    "i32": ("i", 4, "Int32Array"),
}
ALIGN = 8


def _pad(n: int, to: int = ALIGN) -> int:
    return (-n) % to


def write_bundle(rows: list[dict], out_dir: Path, name: str,
                 columns: list[tuple[str, str]]) -> dict:
    """Write one .bin + return its manifest fragment."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bin_path = out_dir / f"{name}.bin"

    entries, offset = [], 0
    buffers: list[bytes] = []
    for col, tcode in columns:
        tc, size, js = TYPES[tcode]
        arr = array.array(tc, (r[col] for r in rows))
        # Wire format is little-endian; every practical target is LE, but be
        # explicit rather than inheriting host order silently.
        if sys.byteorder != "little":
            arr.byteswap()
        raw = arr.tobytes()
        pad = _pad(offset)
        if pad:
            buffers.append(b"\0" * pad)
            offset += pad
        entries.append({"name": col, "type": tcode, "js": js,
                        "offset": offset, "length": len(rows)})
        buffers.append(raw)
        offset += len(raw)

    bin_path.write_bytes(b"".join(buffers))
    return {"file": bin_path.name, "bytes": offset, "columns": entries}


def write_dataset(rows: list[dict], out_dir: Path, sidecars: dict) -> dict:
    """Write core.bin + extended.bin + manifest.json + sidecars."""
    out_dir.mkdir(parents=True, exist_ok=True)
    core_cols = [c for c in COLUMNS if c[0] in CORE_COLUMNS]
    ext_cols = [c for c in COLUMNS if c[0] not in CORE_COLUMNS]

    manifest = {
        "version": 1,
        "rows": len(rows),
        "core": write_bundle(rows, out_dir, "core", core_cols),
        "extended": write_bundle(rows, out_dir, "extended", ext_cols),
    }
    manifest.update({k: v for k, v in sidecars.items() if k == "meta"})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))

    for fname, payload in sidecars.items():
        if fname == "meta":
            continue
        (out_dir / f"{fname}.json").write_text(
            json.dumps(payload, separators=(",", ":")))
    return manifest


def bytes_per_row() -> int:
    return sum(TYPES[t][1] for _, t in COLUMNS)
