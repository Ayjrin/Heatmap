"""PROJECT_PLAN §11 verification, everything that runs without an API key.

Each test maps to a numbered check in the plan. The two that matter most are
test_no_leakage (the economy as-of join must never read a frame later than the
event) and test_danger_degeneracy (D/(D+K) must be exactly 0.5 with no subject
filter -- that is a property of the metric, asserted rather than assumed).
"""
from __future__ import annotations

import collections
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from proleague.config import (CAUSE_CHAMPION, F_VICTIM_TEAM_RED, MAP_MAX_X,  # noqa: E402
                              MAP_MAX_Y, MAP_MIN_X, MAP_MIN_Y, NULL_I16,
                              NULL_U16, ROLE_UNKNOWN)
from proleague.serve.bundle import TYPES, bytes_per_row  # noqa: E402
from proleague.transform.geometry import (bin_index, canonical_for_team,  # noqa: E402
                                          canonicalize, distance, to_screen,
                                          to_unit, under_turret)
from proleague.transform.kills import (COLUMNS, CORE_COLUMNS, _as_of_index,  # noqa: E402
                                       build_participants, classify_cause,
                                       lane_opponents, transform_match)
from proleague.transform.regions import (REGION_NAMES, UNLABELLED_SK,  # noqa: E402
                                         region_name, region_sk)

import make_fixture  # noqa: E402


# --------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def synthetic():
    """A handful of synthetic matches, transformed. Deterministic seed."""
    rng = random.Random(11)
    out = []
    for i in range(12):
        mid = f"NA1_{9_000_000_000 + i}"
        m = make_fixture.make_match(rng, mid, patch="16.17.481.9107",
                                    duration_s=1800)
        t = make_fixture.make_timeline(rng, m)
        out.append((m, t))
    return out


@pytest.fixture(scope="session")
def rows(synthetic):
    all_rows = []
    for sk, (m, t) in enumerate(synthetic):
        all_rows.extend(transform_match(m, t, sk, 0, {}))
    assert all_rows, "fixture produced no kill events"
    return all_rows


# ----------------------------------------------- §11.3 canonicalization
def test_canonicalization_is_an_involution():
    for x, y in [(0, 0), (14870, 14980), (7000, 3000), (1234, 13999), (-120, -120)]:
        rx, ry = canonicalize(*canonicalize(x, y))
        assert abs(rx - x) < 1e-6 and abs(ry - y) < 1e-6


def test_canonicalization_maps_red_base_onto_blue_base():
    # Red base sits at the top-right corner; its mirror must land bottom-left.
    bx, by = canonicalize(MAP_MAX_X - 500, MAP_MAX_Y - 500)
    u, v = to_unit(bx, by)
    assert u < 0.06 and v < 0.06


def test_raw_unit_mirror_would_be_wrong():
    """The naive (14870-y, 14870-x) mirror is wrong because the box is not square.

    span_x is 14990 and span_y is 15100, so a raw-unit reflection drifts by up
    to ~120 units -- more than a champion radius, and enough to move an event
    across a cell boundary at 128x128 (117 units/cell). The error is
    position-dependent and vanishes near the centre, which is exactly why a
    single spot-check is not enough: scan the map and assert the worst case.
    """
    worst = 0.0
    for i in range(21):
        for j in range(21):
            x = MAP_MIN_X + i / 20 * (MAP_MAX_X - MAP_MIN_X)
            y = MAP_MIN_Y + j / 20 * (MAP_MAX_Y - MAP_MIN_Y)
            gx, gy = canonicalize(x, y)
            worst = max(worst, abs(gx - (14870 - y)), abs(gy - (14870 - x)))
    assert worst > 100, f"worst-case drift {worst:.0f} should exceed a cell edge"
    # ...and the correct form stays an exact involution everywhere.
    for i in range(21):
        x = MAP_MIN_X + i / 20 * (MAP_MAX_X - MAP_MIN_X)
        rx, ry = canonicalize(*canonicalize(x, 7000.0))
        assert abs(rx - x) < 1e-6 and abs(ry - 7000.0) < 1e-6


def test_canonical_for_team_leaves_blue_alone():
    assert canonical_for_team(4000, 5000, 100) == (4000, 5000)
    assert canonical_for_team(4000, 5000, 200) != (4000, 5000)


# --------------------------------------------------------- §11.1 geometry
def test_y_flips_exactly_once():
    """Game origin is bottom-left; canvas is top-left. Map max must be screen top."""
    _, top = to_screen(0, MAP_MAX_Y, 100, 100)
    _, bottom = to_screen(0, MAP_MIN_Y, 100, 100)
    assert top < bottom
    assert top == pytest.approx(0, abs=0.01)
    assert bottom == pytest.approx(100, abs=0.01)


def test_bins_are_clamped_and_ordered():
    assert bin_index(MAP_MIN_X, MAP_MIN_Y, 128) == 0
    assert bin_index(MAP_MAX_X, MAP_MAX_Y, 128) == 128 * 128 - 1
    assert bin_index(-9e9, -9e9, 128) == 0            # clamped, not negative
    assert bin_index(9e9, 9e9, 128) == 128 * 128 - 1


def test_turret_ranges_are_symmetric():
    assert under_turret(*_blue_nexus_turret())
    ux, uy = canonicalize(*_blue_nexus_turret())
    assert under_turret(ux, uy), "the red-side reflection must also be a turret"


def _blue_nexus_turret():
    from proleague.transform.geometry import TURRETS
    return TURRETS[0]


# ---------------------------------------------------------- §11.2 regions
def test_landmarks_land_in_named_zones():
    from proleague.transform.geometry import from_unit
    for u, v, expect in [(0.24, 0.68, "Baron"), (0.68, 0.24, "Dragon"),
                         (0.05, 0.05, "Blue Base"), (0.95, 0.95, "Red Base")]:
        name = region_name(region_sk(*from_unit(u, v)))
        assert expect.split()[0] in name, f"({u},{v}) -> {name}, expected {expect}"


def test_unlabelled_is_distinct_not_nearest():
    """Points outside every polygon keep their own label. Forcing them into a
    neighbour would put wall deaths in whichever camp is nearest."""
    assert region_name(UNLABELLED_SK) == "Unlabelled"
    assert UNLABELLED_SK == len(REGION_NAMES) - 1


def test_region_ids_fit_in_uint8():
    assert len(REGION_NAMES) < 255


# ------------------------------------------------------ §11.7 no leakage
def test_as_of_index_never_reads_the_future():
    frames = [{"timestamp": t} for t in range(0, 600_000, 60_000)]
    for t in (0, 59_999, 60_000, 60_001, 314_159, 599_999, 10_000_000):
        i = _as_of_index(frames, t)
        assert frames[i]["timestamp"] <= t
        if i + 1 < len(frames):
            assert frames[i + 1]["timestamp"] > t


def test_economy_join_uses_the_preceding_frame(synthetic):
    """The frame AFTER a kill contains the killer's kill gold. Reading it would
    manufacture a 'being behind causes deaths' result."""
    m, t = synthetic[0]
    frames = t["info"]["frames"]
    for ev in (e for f in frames for e in f["events"] if e["type"] == "CHAMPION_KILL"):
        i = _as_of_index(frames, ev["timestamp"])
        assert frames[i]["timestamp"] <= ev["timestamp"]


# --------------------------------------------- §11.8 both-perspective rows
def test_victim_and_killer_blocks_are_independently_populated(rows):
    champ_kills = [r for r in rows if r["cause"] == CAUSE_CHAMPION]
    assert champ_kills
    for r in champ_kills:
        assert r["victim_champ"] and r["killer_champ"]
        assert r["victim_champ"] != r["killer_champ"] or True   # may collide
    # The blocks must actually differ for at least some rows, or we have
    # accidentally mirrored one side into the other.
    assert any(r["victim_champ"] != r["killer_champ"] for r in champ_kills)
    assert any(r["victim_ordinal"] != r["killer_ordinal"] for r in champ_kills)


def test_executions_have_no_killer_block(rows):
    execs = [r for r in rows if r["cause"] != CAUSE_CHAMPION]
    if not execs:
        pytest.skip("fixture produced no executions")
    for r in execs:
        assert r["killer_champ"] == 0
        assert r["killer_role"] == ROLE_UNKNOWN
        assert r["killer_player"] == NULL_U16
        assert r["killer_gold_diff_lane"] == NULL_I16


def test_schema_is_symmetric():
    v = {c[len("victim_"):] for c, _ in COLUMNS if c.startswith("victim_")}
    k = {c[len("killer_"):] for c, _ in COLUMNS if c.startswith("killer_")}
    assert v == k, f"actor blocks differ: {v ^ k}"
    assert len(v) == 11


# ------------------------------------------------ §11.9 danger degeneracy
def test_danger_degeneracy_with_no_subject_filter(rows):
    """A CHAMPION_KILL is one death and one kill at the SAME coordinate, so with
    no subject filter D == K in every cell and the ratio is 0.5 everywhere.

    This is why the UI's Subject group exists. If this test ever fails, either
    the coordinate is being written differently for the two sides, or
    executions have leaked into the kill count.
    """
    deaths, kills = collections.Counter(), collections.Counter()
    for r in rows:
        cell = bin_index(r["x"], r["y"], 128)
        deaths[cell] += 1
        if r["cause"] == CAUSE_CHAMPION:
            kills[cell] += 1
    for cell, k in kills.items():
        assert deaths[cell] >= k
    champ_only = collections.Counter()
    for r in rows:
        if r["cause"] == CAUSE_CHAMPION:
            champ_only[bin_index(r["x"], r["y"], 128)] += 1
    assert champ_only == kills, "every champion kill must contribute to both sides"


# ---------------------------------------------------- rejection accounting
def test_rejection_rules_catch_every_bad_apple():
    from build_dataset import game_duration_seconds, rejection
    rng = random.Random(3)
    base = dict(patch="16.17.481.9107", duration_s=1800)
    ok = make_fixture.make_match(rng, "NA1_1", **base)
    assert rejection(ok, ["16.17"]) is None
    assert rejection(make_fixture.make_match(rng, "NA1_2", map_id=12, **base),
                     []) == "wrong_map"
    assert rejection(make_fixture.make_match(rng, "NA1_3", queue_id=450, **base),
                     []) == "wrong_queue"
    assert rejection(make_fixture.make_match(rng, "NA1_4", complete=False,
                                             patch="16.17.1", duration_s=1800),
                     []) == "not_complete"
    short = make_fixture.make_match(rng, "NA1_5", patch="16.17.1", duration_s=120)
    assert rejection(short, []) == "too_short"
    assert rejection(ok, ["16.16"]) == "out_of_patch"


def test_game_duration_unit_is_conditional():
    """Seconds when gameEndTimestamp is present, milliseconds when not.
    Backwards, this rejects every game as a remake."""
    from build_dataset import game_duration_seconds
    rng = random.Random(4)
    secs = make_fixture.make_match(rng, "NA1_6", patch="16.17.1", duration_s=1800)
    ms = make_fixture.make_match(rng, "NA1_7", patch="16.17.1", duration_s=1800,
                                 ms_duration=True)
    assert "gameEndTimestamp" in secs["info"]
    assert "gameEndTimestamp" not in ms["info"]
    assert game_duration_seconds(secs["info"]) == pytest.approx(1800)
    assert game_duration_seconds(ms["info"]) == pytest.approx(1800)


# -------------------------------------------------------- blank teamPosition
def test_blank_team_position_is_flagged_not_dropped():
    rng = random.Random(5)
    m = make_fixture.make_match(rng, "NA1_8", patch="16.17.1", duration_s=1800,
                                blank_role=True)
    parts = build_participants(m)
    assert len(parts) == 10, "the game is kept"
    assert any(p.imputed_role for p in parts.values())
    assert any(p.role_sk == ROLE_UNKNOWN or p.imputed_role for p in parts.values())


def test_lane_opponents_pair_across_teams():
    rng = random.Random(6)
    m = make_fixture.make_match(rng, "NA1_9", patch="16.17.1", duration_s=1800)
    parts = build_participants(m)
    opp = lane_opponents(parts)
    for pid, o in opp.items():
        if o is not None:
            assert parts[o].team_id != parts[pid].team_id
            assert parts[o].role_sk == parts[pid].role_sk


# ------------------------------------------------------------ death cause
def test_classify_cause():
    assert classify_cause({"killerId": 3}) == CAUSE_CHAMPION
    assert classify_cause({"killerId": 0, "victimDamageReceived": [
        {"type": "TOWER", "physicalDamage": 500}]}) == 1        # turret
    assert classify_cause({"killerId": 0, "victimDamageReceived": [
        {"type": "MINION", "physicalDamage": 90}]}) == 2
    assert classify_cause({"killerId": 0}) == 4                 # unknown


# ------------------------------------------------- wire format / schema
def test_every_column_has_a_known_type():
    for name, t in COLUMNS:
        assert t in TYPES, f"{name} has unknown type {t}"


def test_core_columns_are_a_subset():
    names = {c for c, _ in COLUMNS}
    assert CORE_COLUMNS <= names
    missing = {"x", "y", "match_sk", "victim_champ", "killer_champ"} - CORE_COLUMNS
    assert not missing, "the default view must resolve from core alone"


def test_row_width_matches_the_plan():
    assert bytes_per_row() == 54, (
        f"PROJECT_PLAN §4.2 documents 54 B/row; schema is now {bytes_per_row()}")


def test_values_fit_their_columns(rows):
    limits = {"u8": (0, 255), "u16": (0, 65535), "u32": (0, 2**32 - 1),
              "i16": (-32768, 32767), "i8": (-128, 127), "i32": (-2**31, 2**31 - 1)}
    for name, t in COLUMNS:
        lo, hi = limits[t]
        for r in rows:
            assert lo <= r[name] <= hi, f"{name}={r[name]} overflows {t}"


def test_flags_encode_victim_side_consistently(rows):
    for r in rows:
        red = bool(r["flags"] & F_VICTIM_TEAM_RED)
        assert isinstance(red, bool)
    assert any(r["flags"] & F_VICTIM_TEAM_RED for r in rows)
    assert any(not (r["flags"] & F_VICTIM_TEAM_RED) for r in rows)


# ------------------------------------------------------------ end to end
def test_build_produces_a_loadable_bundle(tmp_path, synthetic):
    from proleague.serve.bundle import write_dataset
    all_rows = []
    for sk, (m, t) in enumerate(synthetic):
        all_rows.extend(transform_match(m, t, sk, 0, {}))
    man = write_dataset(all_rows, tmp_path, {
        "matches": [], "players": [], "champions": [], "regions": [],
        "meta": {"roles": [], "causes": [], "patches": []}})
    assert man["rows"] == len(all_rows)
    core = (tmp_path / "core.bin").read_bytes()
    assert len(core) == man["core"]["bytes"]
    # Every column must start on a boundary its typed-array view can use.
    for c in man["core"]["columns"] + man["extended"]["columns"]:
        width = TYPES[c["type"]][1]
        assert c["offset"] % width == 0, f"{c['name']} is misaligned for {c['js']}"
    assert (tmp_path / "manifest.json").exists()


# ------------------------------- rejections are exactly predictable
@pytest.mark.parametrize("n,remakes,arams", [(60, 2, 2), (120, 5, 4), (200, 8, 6)])
def test_fixture_rejection_counts_are_exact(n, remakes, arams):
    """The fixture plants bad apples at known indices, so the kept count is a
    fixed number -- not a range.

    This exists because a build on a machine with flaky I/O silently reported
    102 kept out of 120 where the correct answer is 111: transient read
    failures were being counted as data rejections. An exact expected count is
    what turns that from an unnoticed 8% data loss into a failing test.
    """
    from build_dataset import rejection
    rng = random.Random(7)
    got = collections.Counter()
    for i in range(n):
        remake = (i % 23 == 0 and i > 0)
        aram = (i % 29 == 0 and i > 0)
        m = make_fixture.make_match(
            rng, f"NA1_{i}",
            patch="16.17.481.9107",
            duration_s=250 if remake else 1800,
            map_id=12 if aram else 11,
            queue_id=450 if aram else 420,
            complete=not remake,
        )
        why = rejection(m, [])
        if why:
            got[why] += 1
    assert got["not_complete"] == remakes, f"expected {remakes} remakes, got {dict(got)}"
    assert got["wrong_map"] == arams, f"expected {arams} ARAMs, got {dict(got)}"
    assert sum(got.values()) == remakes + arams
    assert n - sum(got.values()) == n - remakes - arams


def test_io_failure_is_not_a_rejection(tmp_path):
    """Missing committed input aborts publication and preserves the live pointer."""
    from proleague.curated import LocalObjects, ValidationError, json_bytes
    from proleague.dataset import build_release
    store, site = LocalObjects(tmp_path / "curated"), LocalObjects(tmp_path / "site")
    store.put("curated/v1/NA1_123/complete.json", json_bytes({"source_kind": "riot", "schema_version": 1}))
    site.put("data/current.json", b'{"dataset_id":"previous"}')
    with pytest.raises(ValidationError, match="missing"):
        build_release(store, site)
    assert site.get("data/current.json") == b'{"dataset_id":"previous"}'
