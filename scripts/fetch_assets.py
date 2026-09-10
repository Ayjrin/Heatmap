#!/usr/bin/env python3
"""Download pinned Riot artwork and champion names; never downloads game data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

import requests

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    assets = ROOT / "web" / "assets"
    version = (assets / "DDRAGON_VERSION").read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("Invalid pinned Data Dragon version")
    base = f"https://ddragon.leagueoflegends.com/cdn/{version}"
    for name, suffix in (("champions.json", "data/en_US/champion.json"),
                         ("map11.png", "img/map/map11.png")):
        response = requests.get(f"{base}/{suffix}", timeout=(10, 45))
        response.raise_for_status()
        data = response.content
        if name.endswith("json"):
            payload = response.json()
            if not payload.get("data") or any(
                "key" not in c or "name" not in c for c in payload["data"].values()
            ):
                raise SystemExit("Invalid champion catalog")
            data = json.dumps(payload, separators=(",", ":")).encode()
        elif not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SystemExit("Invalid map image")
        tmp = assets / f"{name}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, assets / name)
        print(f"Pinned {name}: {version}, {len(data):,} bytes")


if __name__ == "__main__":
    main()
