#!/usr/bin/env python3
"""Rebuild a live browser release from committed curated Parquet and context."""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from proleague.config import Config  # noqa: E402
from proleague.curated import game_duration_seconds, rejection  # noqa: E402,F401
from proleague.dataset import build_release  # noqa: E402
from proleague.pipeline import runtime  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    state, store, site, _ = runtime(Config.load(), args.local)
    run_id = str(uuid.uuid4())
    state.claim(run_id)
    # The claim excludes collection for as long as the rebuild runs, so it needs
    # a run record to go with it. A lock owning no run leaves the status API
    # nothing to report and the page no way to explain its disabled buttons.
    started = int(time.time())
    state.put("RUN#" + run_id, {"run_id": run_id, "mode": "rebuild", "status": "running",
                                "stage": "rebuilding", "new_games": 0, "target": None,
                                "started_at": started, "updated_at": started,
                                "heartbeat_at": started})
    state.put("LATEST", {"run_id": run_id})
    try:
        published = build_release(store, site, run_id=run_id,
                                  before_publish=lambda: state.heartbeat(run_id))
        state.put("PUBLISHED", dict(published, run_id=run_id, updated_at=int(time.time())))
        state.update("RUN#" + run_id, status="succeeded", stage="complete",
                     published_version=published["dataset_id"], updated_at=int(time.time()))
        print(json.dumps(published))
    except BaseException:
        # Ctrl-C included: a rebuild that stops without saying so reads as one
        # still running, and the release it did not reach stays unexplained.
        state.update("RUN#" + run_id, status="failed", stage="rebuild_failed",
                     updated_at=int(time.time()))
        raise
    finally:
        state.release(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
