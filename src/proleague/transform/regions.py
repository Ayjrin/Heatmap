"""Hand-authored named zones for Summoner's Rift.

A heatmap is a picture; a zone label is a sentence. This is the layer that lets
the app say "38% of Challenger jungler deaths happen in the enemy raptor /
red-buff quadrant" instead of just glowing, and it is where statistical claims
are made (33 zones is a tractable FDR correction; 16,384 cells is not).

Polygons are authored in NORMALIZED coordinates (unit square, origin
bottom-left = blue base, so (1,1) is the red base). Normalized authoring keeps
them correct on the non-square coordinate box and makes the blue/red symmetry
literally a reflection of the same list.

Approximate by construction -- drawn to the Rift's well-known layout, not
surveyed from game files. Used for labelling and rollups only; nothing numeric
depends on their exact edges.

Pure stdlib: scalar point-in-polygon, called once per event in the transform.
"""
from __future__ import annotations

from .geometry import to_unit

# Each entry: (name, [(u, v), ...]) traced in the unit square.
# The anti-diagonal runs bottom-left -> top-right; blue is bottom-left.
_BLUE_SIDE: list[tuple[str, list[tuple[float, float]]]] = [
    ("Blue Base",            [(0.00, 0.00), (0.20, 0.00), (0.20, 0.13), (0.13, 0.20), (0.00, 0.20)]),
    ("Blue Top Lane",        [(0.00, 0.20), (0.10, 0.20), (0.10, 0.72), (0.00, 0.72)]),
    ("Blue Bot Lane",        [(0.20, 0.00), (0.72, 0.00), (0.72, 0.10), (0.20, 0.10)]),
    ("Blue Mid Lane",        [(0.20, 0.13), (0.44, 0.37), (0.37, 0.44), (0.13, 0.20)]),
    ("Blue Top Jungle",      [(0.10, 0.20), (0.34, 0.20), (0.40, 0.42), (0.24, 0.60), (0.10, 0.60)]),
    ("Blue Bot Jungle",      [(0.20, 0.10), (0.60, 0.10), (0.60, 0.24), (0.42, 0.40), (0.20, 0.34)]),
    ("Blue Buff (Golems)",   [(0.24, 0.36), (0.34, 0.36), (0.34, 0.46), (0.24, 0.46)]),
    ("Blue Raptors",         [(0.30, 0.24), (0.39, 0.24), (0.39, 0.33), (0.30, 0.33)]),
    ("Blue Wolves",          [(0.17, 0.34), (0.26, 0.34), (0.26, 0.43), (0.17, 0.43)]),
    ("Blue Gromp",           [(0.10, 0.46), (0.19, 0.46), (0.19, 0.55), (0.10, 0.55)]),
    ("Blue Krugs",           [(0.42, 0.12), (0.51, 0.12), (0.51, 0.21), (0.42, 0.21)]),
    ("Blue Red Buff",        [(0.36, 0.24), (0.46, 0.24), (0.46, 0.34), (0.36, 0.34)]),
    ("Blue Tri-brush (top)", [(0.06, 0.60), (0.16, 0.60), (0.16, 0.70), (0.06, 0.70)]),
    ("Blue Tri-brush (bot)", [(0.60, 0.06), (0.70, 0.06), (0.70, 0.16), (0.60, 0.16)]),
]

# Zones straddling the anti-diagonal, belonging to neither side.
_NEUTRAL: list[tuple[str, list[tuple[float, float]]]] = [
    ("Baron Pit",  [(0.16, 0.62), (0.30, 0.62), (0.34, 0.74), (0.20, 0.76)]),
    ("Dragon Pit", [(0.62, 0.16), (0.76, 0.20), (0.74, 0.34), (0.62, 0.30)]),
    ("Top River",  [(0.00, 0.72), (0.10, 0.72), (0.62, 0.98), (0.62, 1.00), (0.00, 0.88)]),
    ("Bot River",  [(0.72, 0.00), (0.88, 0.00), (1.00, 0.62), (0.98, 0.62), (0.72, 0.10)]),
    ("Mid River",  [(0.37, 0.44), (0.44, 0.37), (0.63, 0.56), (0.56, 0.63)]),
]

UNLABELLED = "Unlabelled"


def _mirror(name: str, poly):
    """Reflect a blue-side polygon about the anti-diagonal to get its red twin."""
    return name.replace("Blue", "Red"), [(1.0 - v, 1.0 - u) for (u, v) in poly]


def build_regions() -> list[tuple[str, list[tuple[float, float]]]]:
    """Full zone list: blue side, its mirrored red twin, and the neutral zones.

    Ordered most-specific first (camps before the jungle quadrant containing
    them) because lookup takes the first polygon that contains the point.
    """
    broad_words = ("Jungle", "Base", "Lane")
    specific = [z for z in _BLUE_SIDE if not any(w in z[0] for w in broad_words)]
    broad = [z for z in _BLUE_SIDE if any(w in z[0] for w in broad_words)]
    regions: list[tuple[str, list[tuple[float, float]]]] = []
    for name, poly in specific + broad:
        regions.append((name, poly))
        regions.append(_mirror(name, poly))
    regions.extend(_NEUTRAL)
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

    Points outside every polygon are genuinely outside the zones we named
    (walls, far corners) and keep a distinct label rather than being forced
    into a neighbour -- forcing would put wall deaths in whichever camp happens
    to be nearest and quietly corrupt every per-zone claim.
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
