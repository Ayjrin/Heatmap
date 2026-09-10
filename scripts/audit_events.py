#!/usr/bin/env python3
"""Audit live timeline field counts in memory and print aggregate JSON.

Raw timelines and example events are discarded. To document an observed
finding, quote the aggregate counts in README.md with the run date.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from proleague.config import Config  # noqa: E402
from proleague.curated import CURATED_PREFIX  # noqa: E402
from proleague.extract.riot_client import RiotClient  # noqa: E402
from proleague.extract.routing import Region  # noqa: E402
from proleague.pipeline import load_key, runtime  # noqa: E402


def audit(timelines):
    populated, counts = defaultdict(Counter), Counter()
    matches = 0
    for timeline in timelines:
        matches += 1
        for frame in timeline.get("info", {}).get("frames", []):
            for event in frame.get("events", []):
                kind = event.get("type", "<missing>")
                counts[kind] += 1
                for field, value in event.items():
                    if value is not None:
                        populated[kind][field] += 1
    return {"source_kind": "riot", "matches": matches, "event_counts": dict(counts),
            "populated_fields": {k: dict(v) for k, v in populated.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", type=int, default=5)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.matches <= 50:
        parser.error("--matches must be between 1 and 50")
    cfg = Config.load()
    _, store, _, cloud = runtime(cfg, args.local)
    ids = [key.split("/")[-2] for key in store.keys(CURATED_PREFIX)
           if key.endswith("/complete.json")][:args.matches]
    if not ids:
        parser.error("Collect live games before auditing their timelines")
    client = RiotClient(load_key(cfg, cloud))
    def timelines():
        for mid in ids:
            timeline = client.timeline(Region(cfg.region), mid)
            if timeline is None:
                raise RuntimeError("An audited timeline is temporarily unavailable")
            yield timeline
    print(json.dumps(audit(timelines()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
