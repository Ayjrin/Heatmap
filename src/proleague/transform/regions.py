"""Hand-authored named zones for Summoner's Rift.

A heatmap is a picture; a zone label is a sentence. This is the layer that lets
the app say "38% of Challenger jungler deaths happen in the enemy raptor /
red-buff quadrant" instead of just glowing, and it is where statistical claims
are made (33 zones is a tractable FDR correction; 16,384 cells is not).

Polygons are authored in NORMALIZED coordinates (unit square, origin
bottom-left = blue base, so (1,1) is the red base). Normalized authoring keeps
them correct on the non-square coordinate box and makes the blue/red symmetry
literally a reflection of the same list.

The broad zones TILE the map: base, lanes, jungle quadrants and river together
cover the unit square, so an event only comes back unlabelled if its
coordinates fall outside the map box entirely. Camps and pits are small
polygons checked first, carved out of the broad zone containing them.

Approximate by construction -- drawn to the Rift's well-known layout, not
surveyed from game files. Used for labelling and rollups only; nothing numeric
depends on their exact edges.

Pure stdlib: scalar point-in-polygon, called once per event in the transform.
"""
from __future__ import annotations

from .geometry import to_unit

# -- the two diagonals -------------------------------------------------------
# The Rift is built on two diagonals: mid lane runs along the main diagonal and
# the river along the anti-diagonal. Every broad boundary below is a line in
#   d = u + v - 1   across the river; negative is the blue half
#   s = v - u       along the river; positive is the top half
# which is why the zones tile without gaps -- they are half-planes in (d, s),
# not independently eyeballed boxes.
RIVER = 0.09       # half-width of the river band, measured in d
MID = 0.07         # half-width of the mid-lane band, measured in s
LANE = 0.13        # outer lane band: blue top lane is everything left of it
MID_RIVER = 0.22   # |s| inside which the river reads as mid, not top/bot


def _ds(d: float, s: float) -> tuple[float, float]:
    """(d, s) -> (u, v). The inverse of d = u + v - 1, s = v - u."""
    return (1.0 + d - s) / 2.0, (1.0 + d + s) / 2.0


# Camps and brushes: small, hand-placed, and checked before the broad zones
# they sit inside. Each entry: (name, [(u, v), ...]) traced in the unit square.
_BLUE_CAMPS: list[tuple[str, list[tuple[float, float]]]] = [
    ("Blue Buff (Golems)",   [(0.24, 0.36), (0.34, 0.36), (0.34, 0.46), (0.24, 0.46)]),
    ("Blue Raptors",         [(0.30, 0.24), (0.39, 0.24), (0.39, 0.33), (0.30, 0.33)]),
    ("Blue Wolves",          [(0.17, 0.34), (0.26, 0.34), (0.26, 0.43), (0.17, 0.43)]),
    ("Blue Gromp",           [(0.10, 0.46), (0.19, 0.46), (0.19, 0.55), (0.10, 0.55)]),
    ("Blue Krugs",           [(0.42, 0.12), (0.51, 0.12), (0.51, 0.21), (0.42, 0.21)]),
    ("Blue Red Buff",        [(0.36, 0.24), (0.46, 0.24), (0.46, 0.34), (0.36, 0.34)]),
    ("Blue Tri-brush (top)", [(0.06, 0.60), (0.16, 0.60), (0.16, 0.70), (0.06, 0.70)]),
    ("Blue Tri-brush (bot)", [(0.60, 0.06), (0.70, 0.06), (0.70, 0.16), (0.60, 0.16)]),
]

# Broad zones. These tile the blue half (d <= 0) between them, so nothing
# between the camps is left over. They overlap near the base corner on purpose:
# the base is listed first and wins, which keeps the lane strips describable as
# a single half-plane instead of a notched polygon.
_BLUE_AREAS: list[tuple[str, list[tuple[float, float]]]] = [
    # Quarter disc of radius ~0.36 around the nexus, traced to the base
    # structure on the minimap: fountain, nexus and both nexus turrets.
    ("Blue Base",       [(0.00, 0.00), (0.36, 0.00), (0.33, 0.14),
                         (0.25, 0.25), (0.14, 0.33), (0.00, 0.36)]),
    # Lane strips run the full length of their edge and stop at the
    # anti-diagonal, where the red half's mirrored strip picks them up. That is
    # what makes the corner where the river meets the lane read as lane.
    ("Blue Top Lane",   [(0.00, 0.00), (LANE, 0.00), (LANE, 1.0 - LANE), (0.00, 1.00)]),
    ("Blue Bot Lane",   [(0.00, 0.00), (0.00, LANE), (1.0 - LANE, LANE), (1.00, 0.00)]),
    # Mid lane: |s| <= MID, from the base out to the near bank of the river.
    ("Blue Mid Lane",   [(MID, 0.00), _ds(-RIVER, -MID), _ds(-RIVER, MID), (0.00, MID)]),
    # Each jungle quadrant is the triangle left between lane, mid and river.
    ("Blue Top Jungle", [(LANE, LANE + MID), _ds(-RIVER, MID), (LANE, 1.0 - RIVER - LANE)]),
    ("Blue Bot Jungle", [(LANE + MID, LANE), _ds(-RIVER, -MID), (1.0 - RIVER - LANE, LANE)]),
]

# Zones straddling the anti-diagonal, belonging to neither side. Pits first:
# they sit inside the river band and have to be checked before it.
_PITS: list[tuple[str, list[tuple[float, float]]]] = [
    ("Baron Pit",  [(0.28, 0.66), (0.37, 0.66), (0.37, 0.75), (0.28, 0.75)]),
    ("Dragon Pit", [(0.63, 0.25), (0.72, 0.25), (0.72, 0.34), (0.63, 0.34)]),
]

# The river band |d| <= RIVER, cut into three along s. Drawn out to the map
# corners; the lane strips are checked first and reclaim the corner itself,
# which is where the real river runs into the lane.
_RIVER_ZONES: list[tuple[str, list[tuple[float, float]]]] = [
    ("Top River", [_ds(-RIVER, MID_RIVER), _ds(RIVER, MID_RIVER),
                   (RIVER, 1.00), (0.00, 1.00), (0.00, 1.0 - RIVER)]),
    ("Mid River", [_ds(-RIVER, -MID_RIVER), _ds(-RIVER, MID_RIVER),
                   _ds(RIVER, MID_RIVER), _ds(RIVER, -MID_RIVER)]),
    ("Bot River", [_ds(RIVER, -MID_RIVER), _ds(-RIVER, -MID_RIVER),
                   (1.0 - RIVER, 0.00), (1.00, 0.00), (1.00, RIVER)]),
]

UNLABELLED = "Unlabelled"


def _mirror(name: str, poly):
    """Reflect a blue-side polygon about the anti-diagonal to get its red twin."""
    return name.replace("Blue", "Red"), [(1.0 - v, 1.0 - u) for (u, v) in poly]


# Neighbouring tiles share an edge at an exact decimal, and ray casting has to
# break that tie one way or the other -- a point landing precisely on the seam
# can fall through both. Grow each tile by a hair (at most 40 game units, a
# third of a cell) so they overlap rather than merely touch; list order already
# decides who wins the overlap.
_SEAM = 1.005


def _grow(poly, factor: float = _SEAM):
    """Scale a polygon about its own centroid."""
    cu = sum(p[0] for p in poly) / len(poly)
    cv = sum(p[1] for p in poly) / len(poly)
    return [(cu + (u - cu) * factor, cv + (v - cv) * factor) for u, v in poly]


def build_regions() -> list[tuple[str, list[tuple[float, float]]]]:
    """Full zone list: blue side, its mirrored red twin, and the neutral zones.

    Order is the lookup order -- the first polygon containing the point wins --
    and it matters twice. Camps and pits come before the broad zones that
    contain them. The lane strips come before the river band, so the map corner
    where the river runs into the lane is labelled lane rather than river.
    """
    regions: list[tuple[str, list[tuple[float, float]]]] = []
    for name, poly in _BLUE_CAMPS:
        regions.append((name, poly))
        regions.append(_mirror(name, poly))
    regions.extend(_PITS)
    for name, poly in _BLUE_AREAS:
        regions.append((name, _grow(poly)))
        regions.append(_mirror(name, _grow(poly)))
    regions.extend((name, _grow(poly)) for name, poly in _RIVER_ZONES)
    return regions


REGIONS = build_regions()
REGION_NAMES = [n for n, _ in REGIONS] + [UNLABELLED]
REGION_TO_SK = {n: i for i, n in enumerate(REGION_NAMES)}
UNLABELLED_SK = REGION_TO_SK[UNLABELLED]


def _point_in_poly(u: float, v: float, poly) -> bool:
    """Scalar ray-casting test."""
    inside = False
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if (ay > v) != (by > v):
            x_at_v = (bx - ax) * (v - ay) / (by - ay) + ax
            if u < x_at_v:
                inside = not inside
    return inside


def region_sk(x: float, y: float) -> int:
    """Raw game coords -> region index. UNLABELLED_SK where nothing contains it.

    The broad zones tile the unit square, so an in-bounds event always lands in
    one; a death against a wall is labelled with the quadrant that contains the
    wall, which is honest -- unlike snapping it to the nearest camp, which would
    put wall deaths in whichever camp happens to be closest and quietly corrupt
    every per-camp claim. UNLABELLED is left for coordinates outside the map box.
    """
    u, v = to_unit(x, y)
    for i, (_, poly) in enumerate(REGIONS):
        if _point_in_poly(u, v, poly):
            return i
    return UNLABELLED_SK


def region_name(sk: int) -> str:
    return REGION_NAMES[sk] if 0 <= sk < len(REGION_NAMES) else UNLABELLED


def region_group(name: str) -> str:
    """Coarse grouping for rollups: Lane / Jungle / River / Objective / Base."""
    n = name.lower()
    if "base" in n:
        return "Base"
    if "lane" in n:
        return "Lane"
    if "river" in n:
        return "River"
    if "pit" in n:
        return "Objective"
    if "brush" in n:
        return "Brush"
    if any(k in n for k in ("jungle", "buff", "raptor", "wolves", "gromp", "krugs")):
        return "Jungle"
    return "Other"
