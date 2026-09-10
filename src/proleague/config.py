"""Central configuration. Everything tunable lives here or in config.yaml.

Deliberately dependency-light: this module imports only the stdlib plus PyYAML,
so every CLI entry point starts in well under a second. numpy/pandas/pyarrow are
not on the pipeline's critical path at all (see serve/bundle.py for why the web
payload needs no Arrow library).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


# --- Map geometry -----------------------------------------------------------
# Summoner's Rift (map 11), verified against hextechdocs.dev/map-data. The box
# is asymmetric AND the origin is negative, so nothing may divide by a
# hardcoded 14870 -- every transform normalizes to the unit square first.
MAP_MIN_X = -120
MAP_MIN_Y = -120
MAP_MAX_X = 14870
MAP_MAX_Y = 14980
MAP_SPAN_X = MAP_MAX_X - MAP_MIN_X   # 14990
MAP_SPAN_Y = MAP_MAX_Y - MAP_MIN_Y   # 15100

SR_MAP_ID = 11
SOLO_QUEUE_ID = 420

# Binning. 128 is chosen because Data Dragon's map11.png is 512x512, so one
# cell is exactly 4 image pixels -- power-of-two alignment, no resampling seam.
# ~117 game units/cell, about 1.8 champion diameters.
GRID_SIZE = 128
ROLLUP_SIZES = (128, 64, 32)

# Minimum events before a cell renders rather than being masked, and the
# threshold below which the UI rolls the grid down and says so.
MIN_CELL_EVENTS = 5
THIN_SLICE_EVENTS = 200

# Beta prior pseudo-count for the Danger/Opportunity ratio. k=10 means ten
# imaginary even trades are added to every cell, so a 1-death/0-kill cell reads
# 6/11 rather than 1.00. Smooth in the amount of evidence.
BETA_PSEUDOCOUNT = 10.0

# Sentinel for "no lane opponent" in an int16 column. Arrow-style validity
# bitmaps would force the browser's hot loop to consult a second buffer per
# row; a sentinel keeps each column one contiguous typed array. -32768 is
# unreachable as a real gold or CS differential.
NULL_I16 = -32768
NULL_U8 = 255
NULL_U16 = 65535

# Role enum. 255 = unknown: teamPosition is blank in ~0.9% of ranked games,
# when individualPosition is INVALID (dev-rel #554).
ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_TO_SK = {r: i for i, r in enumerate(ROLES)}
ROLE_UNKNOWN = NULL_U8

# Game-phase buckets, half-open [lo, hi) in minutes.
TIME_BUCKETS = [
    ("0-8", 0, 8),
    ("8-14", 8, 14),
    ("14-20", 14, 20),
    ("20-30", 20, 30),
    ("30+", 30, 10_000),
]

# death_cause. killerId == 0 is an execution by turret/minion/monster, not a
# champion kill -- pooling those inflates turret-adjacent and jungle-camp cells
# with deaths that have no killer, and they have no valid killer block at all.
CAUSE_CHAMPION = 0
CAUSE_TURRET = 1
CAUSE_MINION = 2
CAUSE_MONSTER = 3
CAUSE_UNKNOWN = 4
CAUSE_NAMES = ["champion", "turret", "minion", "monster", "unknown"]

# objectives_up bitmask
OBJ_DRAGON = 1 << 0
OBJ_BARON = 1 << 1
OBJ_HERALD = 1 << 2
OBJ_ELDER = 1 << 3

# flags bitmask on the fact row
F_VICTIM_TEAM_RED = 1 << 0   # 0 = blue (team 100), 1 = red (team 200)
F_VICTIM_WON = 1 << 1
F_FIRST_BLOOD = 1 << 2
F_UNDER_TURRET = 1 << 3
F_TRADE = 1 << 4             # another kill within +/- TRADE_WINDOW_S
F_POSITION_IMPUTED = 1 << 5  # teamPosition was blank; lane diffs are null

TRADE_WINDOW_S = 5
ISOLATION_BUCKET = 100       # units per step in the uint8 isolation column
NEARBY_RADIUS = 2000         # units, for *_enemies_near
SINCE_DEATH_STEP = 4         # seconds per step in the uint8 column
SINCE_OBJECTIVE_STEP = 2     # seconds per step

# Respawn timer, used only to decide who is alive at time T for alive_counts.
# Riot does not expose respawn timers; this is the standard published curve and
# is documented as a modelling assumption. It only gates a count, so a few
# seconds of error shifts a bucket rather than corrupting a measured value.
def respawn_seconds(level: int, minute: float) -> float:
    base = [10, 10, 12, 12, 14, 16, 20, 21, 22, 24, 26, 28, 30, 32.5, 35,
            37.5, 40, 42.5, 45, 47.5][min(max(level, 1), 20) - 1]
    if minute < 15:
        return base
    inc = min((minute - 15) // 5 * 0.05, 0.5) if minute >= 15 else 0.0
    return base * (1 + inc)


MIN_GAME_DURATION_S = 300

# Dev/personal key windows: 20 req/1s AND 100 req/2min. The second binds at
# 3,000 req/hr. Both are enforced (see extract/limiter.py).
DEV_KEY_LIMITS = ((20, 1), (100, 120))
CALLS_PER_MATCH = 2
BUNDLE_SIZE = 25             # matches per bronze object; 25*2 = one minute of budget


@dataclass
class Paths:
    root: Path = REPO_ROOT
    data: Path = REPO_ROOT / "data"

    @property
    def bronze(self) -> Path:
        return self.data / "bronze"

    @property
    def silver(self) -> Path:
        return self.data / "silver"

    @property
    def gold(self) -> Path:
        return self.data / "gold"

    @property
    def state_db(self) -> Path:
        return self.data / "state" / "pipeline.duckdb"

    @property
    def web_data(self) -> Path:
        """Where the built dataset lands for the frontend to serve."""
        return self.root / "web" / "data" / "v1"

    def ladder_dir(self, platform: str, queue: str) -> Path:
        return self.bronze / "raw" / "ladder" / platform / queue

    def match_path(self, match_id: str) -> Path:
        return self.bronze / "raw" / "match" / f"{match_id}.json.gz"

    def timeline_path(self, match_id: str) -> Path:
        return self.bronze / "raw" / "timeline" / f"{match_id}.json.gz"

    def ensure(self) -> None:
        for p in [self.bronze, self.silver, self.gold, self.state_db.parent,
                  self.web_data,
                  self.bronze / "raw" / "match", self.bronze / "raw" / "timeline"]:
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Config:
    # league-v4 is PLATFORM routed, match-v5 is REGIONAL routed. Both explicit
    # rather than derived at call time -- see extract/routing.py.
    platform: str = "na1"
    region: str = "americas"

    tiers: list[str] = field(default_factory=lambda: ["CHALLENGER", "GRANDMASTER"])

    # Patch window. `patches` filters the built dataset; `start_time_epoch`
    # bounds the match-id crawl server-side so we never fetch-then-discard.
    patches: list[str] = field(default_factory=list)
    start_time_epoch: int | None = None

    # The crawl target is NOT a chosen number. We fetch the entire deduped
    # universe discoverable from the ladder; `max_matches` is only a safety cap
    # derived from the 24h budget:
    #   72,000 req/24h - 1,002 discovery  ->  35,499 matches
    # Because the ID universe is shuffled before fetching, stopping early for
    # any reason still yields an unbiased sample.
    max_matches: int = 35_499
    matches_per_player: int = 100

    grid_size: int = GRID_SIZE
    min_sample_warn: int = THIN_SLICE_EVENTS

    paths: Paths = field(default_factory=Paths)

    @property
    def api_key(self) -> str | None:
        return os.environ.get("RIOT_API_KEY")

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        # Local credentials are optional and never override injected task secrets.
        load_dotenv(REPO_ROOT / ".env", override=False)
        path = path or DEFAULT_CONFIG_PATH
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        known = {f for f in cls.__dataclass_fields__ if f != "paths"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                f"{path.name} has unknown keys: {sorted(unknown)}. "
                "Stale keys are how a config silently stops matching the code."
            )
        return cls(**{k: v for k, v in raw.items() if k in known})


PLATFORM_TO_REGION = {
    "br1": "americas", "la1": "americas", "la2": "americas", "na1": "americas",
    "jp1": "asia", "kr": "asia",
    "eun1": "europe", "euw1": "europe", "me1": "europe", "ru": "europe", "tr1": "europe",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}
