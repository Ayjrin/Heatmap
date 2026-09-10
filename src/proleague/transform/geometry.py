"""Map geometry: normalization, side canonicalization, binning, projection.

Scalar and pure-stdlib on purpose. The transform runs per event, not per
column, so numpy buys nothing here and costs a multi-second import on every
CLI invocation.

Coordinate space: Summoner's Rift (map 11) spans min (-120, -120) to max
(14870, 14980). The axes are NOT equal and the origin is negative, so every
transform normalizes to the unit square first rather than dividing by a
hardcoded 14870.
"""
from __future__ import annotations

import math

from ..config import (GRID_SIZE, MAP_MAX_X, MAP_MAX_Y, MAP_MIN_X, MAP_MIN_Y,
                      MAP_SPAN_X, MAP_SPAN_Y, TIME_BUCKETS)


# -- normalized coordinates --------------------------------------------------
def to_unit(x: float, y: float) -> tuple[float, float]:
    """Raw game coords -> unit square [0,1]^2, origin bottom-left."""
    return (x - MAP_MIN_X) / MAP_SPAN_X, (y - MAP_MIN_Y) / MAP_SPAN_Y


def from_unit(u: float, v: float) -> tuple[float, float]:
    """Unit square -> raw game coords."""
    return u * MAP_SPAN_X + MAP_MIN_X, v * MAP_SPAN_Y + MAP_MIN_Y


# -- side canonicalization ---------------------------------------------------
def canonicalize(x: float, y: float) -> tuple[float, float]:
    """Mirror a red-side coordinate onto the blue side.

    The Rift is symmetric about the ANTI-diagonal (blue base bottom-left, red
    base top-right), so the mirror is (u, v) -> (1 - v, 1 - u) in normalized
    space. Doing it in normalized space is what makes it correct on a
    non-square box: `(14870 - y, 14870 - x)` is wrong because
    span_x (14990) != span_y (15100), and lands ~110 units off on one axis.

    This is an involution: canonicalize(canonicalize(p)) == p.
    """
    u, v = to_unit(x, y)
    return from_unit(1.0 - v, 1.0 - u)


def canonical_for_team(x: float, y: float, team_id: int) -> tuple[float, float]:
    """Coords as if the actor were always blue side. 100 = blue, 200 = red.

    Roughly doubles the events per cell without splitting the data, which is
    what makes thin slices survive at 128x128.
    """
    return canonicalize(x, y) if team_id == 200 else (x, y)


# -- binning -----------------------------------------------------------------
def to_bin(x: float, y: float, grid_size: int = GRID_SIZE) -> tuple[int, int]:
    """Raw coords -> integer (bin_x, bin_y), clamped into the grid."""
    u, v = to_unit(x, y)
    bx = min(max(int(u * grid_size), 0), grid_size - 1)
    by = min(max(int(v * grid_size), 0), grid_size - 1)
    return bx, by


def bin_index(x: float, y: float, grid_size: int = GRID_SIZE) -> int:
    """Raw coords -> flat cell index, row-major (by * size + bx)."""
    bx, by = to_bin(x, y, grid_size)
    return by * grid_size + bx


def bin_center(bx: int, by: int, grid_size: int = GRID_SIZE) -> tuple[float, float]:
    """Integer bin -> raw coords of that bin's center."""
    return from_unit((bx + 0.5) / grid_size, (by + 0.5) / grid_size)


def rollup(bx: int, by: int, from_size: int = GRID_SIZE, to_size: int = 64):
    """Coarsen bins by an integer factor -- a right-shift for powers of two."""
    if from_size % to_size:
        raise ValueError(f"{from_size} is not an integer multiple of {to_size}")
    f = from_size // to_size
    return bx // f, by // f


# -- screen projection -------------------------------------------------------
def to_screen(x: float, y: float, width: int, height: int) -> tuple[float, float]:
    """Raw coords -> pixel coords.

    Game origin is bottom-left, canvas origin is top-left, so Y flips. This is
    the ONLY place the flip happens; everything upstream stays in game space.
    """
    u, v = to_unit(x, y)
    return u * width, height * (1.0 - v)


# -- time --------------------------------------------------------------------
def game_minute(timestamp_ms: float) -> float:
    return timestamp_ms / 60_000.0


def time_bucket(minute: float) -> str:
    """Minute -> game-phase label. Half-open [lo, hi)."""
    for label, lo, hi in TIME_BUCKETS:
        if lo <= minute < hi:
            return label
    return TIME_BUCKETS[-1][0]


def distance(x1: float, y1: float, x2: float, y2: float) -> float:
    """Euclidean distance in game units."""
    return math.hypot(x1 - x2, y1 - y2)


# -- static map features -----------------------------------------------------
# Turret positions, normalized. Used for the `under_turret` flag and, more
# importantly, as the map-alignment calibration target: these need no API data,
# so alignment can be verified while the crawl is still running.
TURRETS_BLUE_UNIT = [
    (0.065, 0.428), (0.065, 0.582), (0.065, 0.702),   # top lane
    (0.212, 0.212), (0.297, 0.297), (0.372, 0.372),   # mid lane
    (0.428, 0.065), (0.582, 0.065), (0.702, 0.065),   # bot lane
    (0.118, 0.150), (0.150, 0.118),                   # nexus pair
]
TURRET_RADIUS = 775.0    # game units


def _all_turrets() -> list[tuple[float, float]]:
    out = []
    for u, v in TURRETS_BLUE_UNIT:
        out.append(from_unit(u, v))
        out.append(from_unit(1.0 - v, 1.0 - u))   # red side is the reflection
    return out


TURRETS = _all_turrets()


def under_turret(x: float, y: float, radius: float = TURRET_RADIUS) -> bool:
    """Whether a point is inside any turret's range. Separates dive deaths."""
    return any(distance(x, y, tx, ty) <= radius for tx, ty in TURRETS)
