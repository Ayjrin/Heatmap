"""Release format 3: partitions, rank attribution, prerendered cells, shards."""
import array
import collections
import json
import random
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import make_fixture
import proleague.dataset as dataset
from proleague.config import NULL_U8, NULL_U32, TIER_TO_SK
from proleague.curated import LocalObjects, ingest_match
from proleague.extract.routing import Region
from proleague.transform.geometry import bin_index
from proleague.transform.regions import REGION_NAMES

GAME_START_MS = 1_760_000_000_000
CODES = {"u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i"}


def curated(tmp_path, games):
    """Curate `games` = [(match_id, patch)], each with its own ten PUUIDs."""
    rng = random.Random(5)
    store, site = LocalObjects(tmp_path / "store"), LocalObjects(tmp_path / "site")
    for mid, patch in games:
        match = make_fixture.make_match(rng, mid, patch=patch, duration_s=1800)
        for participant in match["info"]["participants"]:
            participant["puuid"] = f"{mid}-p{participant['participantId'] - 1:02d}"
        timeline = make_fixture.make_timeline(rng, match)
        killed = collections.Counter(event["victimId"] for frame in timeline["info"]["frames"]
                                     for event in frame["events"] if event["type"] == "CHAMPION_KILL")
        for participant in match["info"]["participants"]:
            participant["deaths"] = killed[participant["participantId"]]
        client = collections.namedtuple("C", "match timeline")(lambda *a, **k: match, lambda *a, **k: timeline)
        ingest_match(client, Region("americas"), mid, store, patches=[], run_id="r")
    return store, site


def observation(puuid, tier, division="I", lp=0, at=GAME_START_MS // 1000, source="ladder", history=()):
    record = {"puuid": puuid, "tier": tier, "division": division, "lp": lp,
              "rank_observed_at": at, "rank_source": source}
    if history:
        record["rank_history"] = list(history)
    return record


def roster_for_three_games():
    """NA1_1 is a Diamond cohort, NA1_2 an Emerald cohort, NA1_3 unknown."""
    roster = [observation(f"NA1_1-p{i:02d}", "DIAMOND") for i in range(4)]
    roster += [observation(f"NA1_1-p{i:02d}", "EMERALD") for i in range(4, 8)]
    roster.append(observation("NA1_1-p08", "UNRANKED", None, None, source="lookup"))
    # Emerald now, Diamond when the game was played: the game keeps Diamond.
    roster.append(observation("NA1_2-p00", "EMERALD", "I", 5, at=GAME_START_MS // 1000 + 30 * 86_400,
                              history=[{"at": GAME_START_MS // 1000 - 10, "tier": "DIAMOND", "division": "I", "lp": 80}]))
    roster += [observation(f"NA1_2-p{i:02d}", "EMERALD", "II") for i in range(1, 7)]
    return roster


def columns(site, base, part, kind="core"):
    raw = site.get(f"{base}/{part[kind]['file']}")
    assert len(raw) == part[kind]["bytes"]
    out = {}
    for column in part[kind]["columns"]:
        values = array.array(CODES[column["type"]])
        values.frombytes(raw[column["offset"]:column["offset"] + column["length"] * values.itemsize])
        out[column["name"]] = list(values)
    return out


def released(site, published):
    base = f'data/releases/{published["dataset_id"]}'
    manifest = json.loads(site.get(f"{base}/manifest.json"))
    return base, manifest


def player_index(site, base, manifest):
    ids = []
    for shard in range(manifest["players"]["shards"]):
        ids += json.loads(site.get(f"{base}/players/ids-{shard}.json"))
    return {puuid: sk for sk, puuid in enumerate(ids)}


def test_release_partitions_by_cohort_tier_and_patch_with_as_of_attribution(tmp_path):
    store, site = curated(tmp_path, [("NA1_1", "16.17.1"), ("NA1_2", "16.17.1"), ("NA1_3", "16.17.1"),
                                     ("NA1_4", "16.16.2")])
    published = dataset.build_release(store, site, roster=roster_for_three_games())
    base, manifest = released(site, published)
    assert manifest["version"] == 2 and manifest["meta"]["format"] == 3 and manifest["meta"]["bytes_per_row"] == 60
    parts = {p["key"]: p for p in manifest["parts"]}
    assert set(parts) == {"3-16.17", "4-16.17", "255-16.17", "255-16.16"}
    assert all(p["matches"] == 1 for p in parts.values()) and manifest["meta"]["tiers"] == [3, 4, 255]
    assert manifest["meta"]["unknown_tier_matches"] == 2 and manifest["rows"] == sum(p["rows"] for p in parts.values())
    assert parts["255-16.16"]["patch"] != parts["3-16.17"]["patch"]
    players = player_index(site, base, manifest)

    diamond = columns(site, base, parts["3-16.17"])
    matches = json.loads(site.get(f"{base}/{parts['3-16.17']['matches_file']}"))
    assert matches == [{"id": "NA1_1", "patch": parts["3-16.17"]["patch"], "duration": 1800,
                        "blue_win": matches[0]["blue_win"], "tier": 3}]
    assert set(diamond["match_sk"]) == {0}, "match keys are local to the partition"
    by_victim = collections.defaultdict(set)
    for victim, tier in zip(diamond["victim_player"], diamond["victim_tier"]):
        by_victim[victim].add(tier)
    for i in range(10):
        expected = {3} if i < 4 else {4} if i < 8 else {TIER_TO_SK["UNRANKED"]} if i == 8 else {NULL_U8}
        assert by_victim.get(players[f"NA1_1-p{i:02d}"], expected) == expected, f"NA1_1-p{i:02d}"
    for cause, killer, tier in zip(diamond["cause"], diamond["killer_player"], diamond["killer_tier"]):
        if cause != 0:
            assert (killer, tier) == (NULL_U32, NULL_U8), "executions have no killer rank"
        else:
            assert tier in {3, 4, TIER_TO_SK["UNRANKED"], NULL_U8}

    emerald = columns(site, base, parts["4-16.17"])
    climbed = players["NA1_2-p00"]
    tiers_seen = {tier for victim, tier in zip(emerald["victim_player"], emerald["victim_tier"]) if victim == climbed}
    assert tiers_seen <= {3} and set(emerald["victim_tier"]) <= {3, 4, NULL_U8}
    unknown = columns(site, base, parts["255-16.17"])
    assert set(unknown["victim_tier"]) == {NULL_U8} and set(unknown["killer_tier"]) == {NULL_U8}

    validation = json.loads(site.get(f"{base}/validation.json"))
    assert validation["roster_players"] == 16 and len(validation["attribution_sha256"]) == 64
    assert sorted(p["key"] for p in validation["parts"]) == sorted(parts)
    warehouse = next(k for k in store.keys(f'warehouse/dim_match/dataset_id={published["dataset_id"]}/'))
    tiers = dict(duckdb.sql(f"SELECT match_id, tier FROM read_parquet('{store.root / warehouse}') ORDER BY 1").fetchall())
    assert tiers == {"NA1_1": "DIAMOND", "NA1_2": "EMERALD", "NA1_3": None, "NA1_4": None}
    player_table = next(k for k in store.keys(f'warehouse/dim_player/dataset_id={published["dataset_id"]}/'))
    assert duckdb.sql(f"SELECT count(*) FROM read_parquet('{store.root / player_table}')").fetchone()[0] == 16
    assert sorted(k.split("/", 1)[0] for k in {k[len(base) + 1:].split("/")[0] for k in site.keys(base)}) == sorted(
        ["cells.bin", "champions.json", "manifest.json", "parts", "players", "regions.json", "validation.json"])


def test_prerendered_cells_equal_a_recount_of_the_partition_rows(tmp_path):
    store, site = curated(tmp_path, [("NA1_1", "16.17.1"), ("NA1_2", "16.17.1"), ("NA1_3", "16.17.1")])
    published = dataset.build_release(store, site, roster=roster_for_three_games())
    base, manifest = released(site, published)
    cells = site.get(f"{base}/{manifest['cells']['file']}")
    assert len(cells) == manifest["cells"]["bytes"] and manifest["cells"]["rollups"] == [128, 64, 32]
    assert manifest["cells"]["regions"] == len(REGION_NAMES)
    assert [s["tier"] for s in manifest["cells"]["tiers"]] == [3, 4, 255]
    for section in manifest["cells"]["tiers"]:
        stored = {}
        for column in section["columns"]:
            values = array.array("I")
            values.frombytes(cells[column["offset"]:column["offset"] + column["length"] * 4])
            stored[column["name"]] = list(values)
        rows = []
        for part in manifest["parts"]:
            if part["tier"] != section["tier"]:
                continue
            data = columns(site, base, part)
            rows += [(f"{part['key']}:{data['match_sk'][i]}", data["x"][i], data["y"][i], data["cause"][i],
                      data["region_sk"][i]) for i in range(part["rows"])]
        assert section["rows"] == len(rows) and section["matches"] == 1
        assert section["kills"] == sum(1 for r in rows if r[3] == 0)
        assert section["death_matches"] == len({r[0] for r in rows})
        assert section["kill_matches"] == len({r[0] for r in rows if r[3] == 0})
        deaths, kills = [0] * 128 * 128, [0] * 128 * 128
        for _, x, y, cause, _ in rows:
            cell = bin_index(x, y, 128)
            deaths[cell] += 1
            kills[cell] += cause == 0
        assert stored["deaths_128"] == deaths and stored["kills_128"] == kills
        for size in (128, 64, 32):
            games = collections.defaultdict(set)
            for match, x, y, _, _ in rows:
                games[bin_index(x, y, size)].add(match)
            assert stored[f"games_{size}"] == [len(games[c]) for c in range(size * size)], size
        zone = {name: [0] * len(REGION_NAMES) for name in ("deaths", "kills")}
        zone_games = collections.defaultdict(set)
        for match, _, _, cause, region in rows:
            zone["deaths"][region] += 1
            zone["kills"][region] += cause == 0
            zone_games[region].add(match)
        assert stored["zone_deaths"] == zone["deaths"] and stored["zone_kills"] == zone["kills"]
        assert stored["zone_games"] == [len(zone_games[r]) for r in range(len(REGION_NAMES))]


def test_player_shards_are_sorted_index_aligned_and_carry_tiers(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "PLAYER_SHARD", 8)
    store, site = curated(tmp_path, [("NA1_1", "16.17.1"), ("NA1_2", "16.17.1"), ("NA1_3", "16.17.1")])
    roster = roster_for_three_games()
    published = dataset.build_release(store, site, roster=roster)
    base, manifest = released(site, published)
    players = manifest["players"]
    assert (players["count"], players["shard_size"], players["shards"]) == (30, 8, 4)
    index = json.loads(site.get(f"{base}/{players['index']}"))
    ids, names = [], []
    for shard in range(players["shards"]):
        ids.append(json.loads(site.get(f"{base}/players/ids-{shard}.json")))
        names.append(json.loads(site.get(f"{base}/players/names-{shard}.json")))
    assert [len(s) for s in ids] == [8, 8, 8, 6] and [len(s) for s in names] == [8, 8, 8, 6]
    flat = [p for shard in ids for p in shard]
    assert flat == sorted(flat) and len(set(flat)) == 30
    assert index == {"count": 30, "shard_size": 8, "first_id": [shard[0] for shard in ids]}
    tiers = site.get(f"{base}/{players['tiers']['file']}")
    assert len(tiers) == players["tiers"]["bytes"] == 30
    expected = {r["puuid"]: TIER_TO_SK[r["tier"]] for r in roster}
    assert list(tiers) == [expected.get(p, NULL_U8) for p in flat]
    assert all(name.startswith("Player") and "#" in name for shard in names for name in shard)


def test_dataset_id_changes_only_when_attribution_changes(tmp_path):
    store, site = curated(tmp_path, [("NA1_1", "16.17.1"), ("NA1_2", "16.17.1"), ("NA1_3", "16.17.1")])
    roster = roster_for_three_games()
    first = dataset.build_release(store, site, roster=roster)["dataset_id"]
    assert dataset.build_release(store, site, roster=roster)["dataset_id"] == first
    lp_only = [dict(r, lp=(r["lp"] or 0) + 40, rank_observed_at=r["rank_observed_at"] + 3600) for r in roster]
    assert dataset.build_release(store, site, roster=lp_only)["dataset_id"] == first, "LP and timestamps are not attribution"
    demoted = [dict(r, tier="EMERALD") if r["puuid"] == "NA1_1-p00" else r for r in roster]
    assert dataset.build_release(store, site, roster=demoted)["dataset_id"] != first
    without = dataset.build_release(store, site)
    assert without["dataset_id"] not in {first}
    base, manifest = released(site, without)
    assert [p["key"] for p in manifest["parts"]] == ["255-16.17"] and manifest["parts"][0]["matches"] == 3
    assert manifest["meta"]["tiers"] == [255] and manifest["meta"]["unknown_tier_matches"] == 3
    assert json.loads(site.get("data/current.json"))["dataset_id"] == without["dataset_id"]
