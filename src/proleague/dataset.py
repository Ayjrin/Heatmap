"""Build and atomically publish live datasets from committed curated facts.

Release format 3 is partitioned. Rows live in `parts/<tier>-<patch>/` so the
browser can stream them in the background; `cells.bin` holds the prerendered
default view per tier so the first paint needs no rows at all; player names
and PUUIDs are sharded so a hundred thousand names load only when asked for.
"""
from __future__ import annotations

import array
import gzip
import hashlib
import itertools
import json
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

from .config import (CAUSE_NAMES, GRID_SIZE, NULL_U8, NULL_U32, ROLES, ROLLUP_SIZES,
                     TIER_NAMES, TIER_UNRANKED)
from .curated import (CURATED_PREFIX, FACT_COLUMNS, SCHEMA_VERSION, S3Objects,
                      ValidationError, digest, json_bytes, sql_path)
from .serve.bundle import TYPES, bytes_per_row
from .transform.geometry import bin_index
from .transform.kills import COLUMNS, CORE_COLUMNS, build_participants
from .transform.regions import REGIONS, REGION_NAMES, region_group, region_sk

DIM_MATCH_COLUMNS = [("match_id", "VARCHAR"), ("patch", "VARCHAR"), ("duration", "BIGINT"),
                     ("blue_win", "BOOLEAN"), ("game_start_ms", "BIGINT"), ("collected_at", "BIGINT"),
                     ("tier", "VARCHAR")]
DIM_PARTICIPANT_COLUMNS = [("match_id", "VARCHAR"), ("participant_id", "BIGINT"),
                          ("puuid", "VARCHAR"), ("display_name", "VARCHAR"),
                          ("champion_id", "BIGINT"), ("team_id", "BIGINT"),
                          ("role", "BIGINT"), ("won", "BOOLEAN"),
                          ("tier", "VARCHAR"), ("division", "VARCHAR"), ("rank_observed_at", "BIGINT")]
DIM_PLAYER_COLUMNS = [("puuid", "VARCHAR"), ("tier", "VARCHAR"), ("division", "VARCHAR"),
                      ("lp", "BIGINT"), ("rank_observed_at", "BIGINT"), ("rank_source", "VARCHAR")]
WAREHOUSE_TABLES = ("fact_kill", "dim_match", "dim_participant", "dim_player")

# Bump whenever the release serialization or dictionary semantics change.
RELEASE_FORMAT_VERSION = 3
MANIFEST_VERSION = 2
PLAYER_SHARD = 8192
CELL_GRID = GRID_SIZE
PREFETCH_WORKERS = 16
PREFETCH_DEPTH = 32

# The zone map is content, not code: released labels are only meaningful next to
# the polygons that produced them. Folding its digest into the dataset id means
# editing the map publishes a new immutable release instead of colliding with
# the old one, and no manual version bump is needed to remember that.
ZONE_DIGEST = digest(json_bytes(REGIONS))[:16]


def _make_table(con, name, columns):
    con.execute(f"CREATE TABLE {name} (" + ",".join(f'"{n}" {t}' for n, t in columns) + ")")


def _insert(con, name, rows, width):
    if rows:
        con.executemany(f"INSERT INTO {name} VALUES (" + ",".join("?" for _ in range(width)) + ")", rows)


def _write_column(out, entries, name, kind, values):
    """Append one 8-byte-aligned typed column to an open binary file."""
    pad = (-out.tell()) % 8
    out.write(b"\0" * pad)
    entries.append({"name": name, "type": kind, "js": TYPES[kind][2],
                    "offset": out.tell(), "length": len(values)})
    if sys.byteorder != "little":
        values = array.array(values.typecode, values)
        values.byteswap()
    out.write(values.tobytes())


def _binary(con, path, columns, count, table):
    """Write `columns` of `table` (ordered by its `pos` column) as one .bin."""
    entries = []
    with path.open("wb") as out:
        for name, kind in columns:
            pad = (-out.tell()) % 8
            out.write(b"\0" * pad)
            entries.append({"name": name, "type": kind, "js": TYPES[kind][2],
                            "offset": out.tell(), "length": count})
            cur = con.execute(f'SELECT "{name}" FROM {table} ORDER BY pos')
            while chunk := cur.fetchmany(8192):
                values = array.array(TYPES[kind][0], (row[0] for row in chunk))
                if sys.byteorder != "little":
                    values.byteswap()
                out.write(values.tobytes())
    return {"bytes": path.stat().st_size, "columns": entries}


def _prefetched(keys, fetch, workers=PREFETCH_WORKERS, depth=PREFETCH_DEPTH):
    """Yield fetch(key) in key order with a bounded number of reads in flight.

    Reading two objects per match sequentially is ~100 minutes at 100k
    matches; the pool hides the latency. The window is bounded so the
    working set stays a few dozen matches, never the whole corpus.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending, remaining = deque(), iter(keys)
        for key in itertools.islice(remaining, depth):
            pending.append(pool.submit(fetch, key))
        while pending:
            result = pending.popleft().result()
            following = next(remaining, None)
            if following is not None:
                pending.append(pool.submit(fetch, following))
            yield result


def _roster_observations(roster):
    """(puuid, tier, division, lp, at, source, current) rows from roster records.

    Current rank plus its bounded history. Records that never observed a tier
    carry nothing a release can attribute, so they are skipped.
    """
    rows = []
    for record in roster or []:
        puuid, tier = record.get("puuid"), record.get("tier")
        if not isinstance(puuid, str) or tier not in TIER_NAMES:
            continue
        lp = record.get("lp")
        rows.append((puuid, tier, record.get("division"), int(lp) if lp is not None else None,
                     int(record.get("rank_observed_at") or 0), str(record.get("rank_source") or "ladder"), True))
        for past in record.get("rank_history") or []:
            if past.get("tier") in TIER_NAMES:
                lp = past.get("lp")
                rows.append((puuid, past["tier"], past.get("division"), int(lp) if lp is not None else None,
                             int(past.get("at") or 0), "history", False))
    return rows


def build_release(store, site, *, run_id="rebuild", before_publish=None, roster=None):
    """Validate all curated inputs, upload immutable outputs, then switch pointer.

    DuckDB spills its working set to a temporary directory. Facts and wire
    columns are streamed in bounded batches; raw Riot data is never needed.
    `roster` is the PLAYER# records; without it every game is "Unknown rank".
    """
    from .curated import game_duration_seconds

    keys = sorted(k for k in store.keys(CURATED_PREFIX) if k.endswith("/complete.json"))
    if not keys:
        raise ValidationError("No completed live Riot games are available")
    inventory = []

    def fetch(key):
        prefix = key.rsplit("/", 1)[0] + "/"
        return key, store.get(key), store.get(prefix + "context.json"), store.get(prefix + "facts.parquet")

    with tempfile.TemporaryDirectory(prefix="heatmap-build-") as tmp:
        work = Path(tmp)
        con = duckdb.connect(str(work / "build.duckdb"))
        try:
            con.execute("SET memory_limit='512MB'")
            con.execute(f"SET temp_directory={sql_path(work / 'spill')}")
            _make_table(con, "fact_kill", FACT_COLUMNS)
            _make_table(con, "dim_match", DIM_MATCH_COLUMNS)
            _make_table(con, "dim_participant", DIM_PARTICIPANT_COLUMNS)
            for index, (key, complete_raw, context_raw, facts_raw) in enumerate(_prefetched(keys, fetch)):
                complete = json.loads(complete_raw)
                if complete.get("source_kind") != "riot" or complete.get("schema_version") != SCHEMA_VERSION:
                    raise ValidationError("Publication requires live Riot provenance and the current schema")
                if context_raw is None or facts_raw is None:
                    raise ValidationError("A completed match is missing a required curated object")
                if digest(context_raw) != complete["context_sha256"] or digest(facts_raw) != complete["facts_sha256"]:
                    raise ValidationError(f"Curated input failed its integrity check: {complete['match_id']}")
                context = json.loads(context_raw)
                mid = context["metadata"]["matchId"]
                if context.get("source_kind") != "riot" or mid != complete["match_id"]:
                    raise ValidationError("Curated context provenance or identity is inconsistent")
                # One file per match, never a reused name. DuckDB keys internal
                # per-file state on the path, and overwriting a single scratch
                # filename thousands of times exhausts it: a build died with
                # "Out of buffer" on the 3,314th match at 185MB RSS and 8 open
                # descriptors, and raising memory_limit to 1400MB moved the
                # failure not one match. Unlink as we go so the working set is
                # still one match at a time.
                parquet = work / f"match-{index}.parquet"
                parquet.write_bytes(facts_raw)
                count, invalid = con.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE match_id != ?) FROM read_parquet({sql_path(parquet)})",
                    [mid]).fetchone()
                if invalid or count != complete["row_count"]:
                    raise ValidationError(
                        f"Curated facts do not reconcile with the match commit: {mid} holds {count} rows "
                        f"({invalid} under another match id), commit claims {complete['row_count']}")
                names = ",".join(f'"{n}"' for n, _ in FACT_COLUMNS)
                con.execute(f"INSERT INTO fact_kill SELECT {names} FROM read_parquet({sql_path(parquet)})")
                parquet.unlink()
                info = context["info"]
                parts = build_participants(context)
                blue = next(p for p in parts.values() if p.team_id == 100)
                patch = ".".join(str(info.get("gameVersion", "")).split(".")[:2])
                start = int(info.get("gameStartTimestamp") or info.get("gameCreation") or 0)
                _insert(con, "dim_match", [(mid, patch, int(game_duration_seconds(info)), blue.won,
                                            start, complete["collected_at"], None)], len(DIM_MATCH_COLUMNS))
                _insert(con, "dim_participant", [
                    (mid, p.pid, p.puuid, p.riot_id or f"Player {p.puuid[:8]}",
                     p.champ_id, p.team_id, p.role_sk, p.won, None, None, None) for p in parts.values()],
                    len(DIM_PARTICIPANT_COLUMNS))
                inventory.append({"match_id": mid, "context_sha256": digest(context_raw),
                                  "facts_sha256": digest(facts_raw), "rows": count})

            # -- rank attribution --------------------------------------------
            # Ranks are observed at crawl time, not at match time. Each
            # participant takes the observation nearest the game's start, so a
            # player who climbed between runs keeps the older games in the
            # older tier, and a cohort is the lower median of the known ranks.
            _make_table(con, "tier_map", [("tier", "VARCHAR"), ("sk", "BIGINT")])
            _insert(con, "tier_map", [(t, i) for i, t in enumerate(TIER_NAMES)], 2)
            _make_table(con, "rank_obs", [("puuid", "VARCHAR"), ("tier", "VARCHAR"), ("division", "VARCHAR"),
                                          ("lp", "BIGINT"), ("observed_at", "BIGINT"), ("source", "VARCHAR"),
                                          ("current", "BOOLEAN")])
            observations = _roster_observations(roster)
            for offset in range(0, len(observations), 8192):
                _insert(con, "rank_obs", observations[offset:offset + 8192], 7)
            con.execute("""CREATE TABLE dim_player AS SELECT puuid, tier, division, lp, observed_at AS rank_observed_at,
                source AS rank_source FROM rank_obs WHERE current ORDER BY puuid""")
            con.execute("""CREATE TABLE attribution AS SELECT match_id, participant_id, tier, division, observed_at FROM (
                SELECT dp.match_id, dp.participant_id, ro.tier, ro.division, ro.observed_at,
                       row_number() OVER (PARTITION BY dp.match_id, dp.participant_id
                                          ORDER BY abs(ro.observed_at * 1000 - dm.game_start_ms), ro.observed_at DESC, ro.current DESC) AS rn
                FROM dim_participant dp JOIN dim_match dm USING(match_id) JOIN rank_obs ro ON ro.puuid = dp.puuid)
                WHERE rn = 1""")
            con.execute("""UPDATE dim_participant SET tier = a.tier, division = a.division, rank_observed_at = a.observed_at
                FROM attribution a WHERE dim_participant.match_id = a.match_id
                AND dim_participant.participant_id = a.participant_id""")
            con.execute(f"""CREATE TABLE cohort AS SELECT match_id, tier FROM (
                SELECT dp.match_id, dp.tier, row_number() OVER (PARTITION BY dp.match_id ORDER BY tm.sk) AS rn,
                       count(*) OVER (PARTITION BY dp.match_id) AS n
                FROM dim_participant dp JOIN tier_map tm ON tm.tier = dp.tier
                WHERE dp.tier IS NOT NULL AND dp.tier != '{TIER_UNRANKED}')
                WHERE rn = (n - 1) // 2 + 1""")
            con.execute("UPDATE dim_match SET tier = c.tier FROM cohort c WHERE dim_match.match_id = c.match_id")
            attribution_digest = hashlib.sha256()
            cursor = con.cursor().execute(f"""SELECT dp.match_id, dp.participant_id, coalesce(tm.sk, {NULL_U8})
                FROM dim_participant dp LEFT JOIN tier_map tm ON tm.tier = dp.tier
                ORDER BY dp.match_id, dp.participant_id""")
            while chunk := cursor.fetchmany(8192):
                for mid, pid, sk in chunk:
                    attribution_digest.update(f"{mid}\t{pid}\t{sk}\n".encode())
            attribution_sha256 = attribution_digest.hexdigest()

            # -- geometry ------------------------------------------------------
            # A zone label is geometry, not collected data, so re-derive it from
            # the stored coordinates instead of trusting whatever zone map was
            # current when the game was crawled. Raw Riot responses are never
            # kept, so without this a change to the zone map could only ever
            # apply to games collected after it -- the labels on everything
            # already in the store would be frozen. The 128-grid cell index is
            # computed here too, once, so the prerendered cells and the browser
            # bin every event identically.
            #
            # Scalar Python, streamed through its own cursor so the coordinates
            # stay in bounded batches. A DuckDB UDF would be neater but drags in
            # numpy, which this project stays clear of.
            _make_table(con, "zone_map", [("match_id", "VARCHAR"), ("frame_index", "BIGINT"),
                                          ("event_index", "BIGINT"), ("region_sk", "BIGINT"), ("cell128", "BIGINT")])
            coords = con.cursor().execute("SELECT match_id, frame_index, event_index, x, y FROM fact_kill")
            while chunk := coords.fetchmany(8192):
                _insert(con, "zone_map", [(mid, fi, ei, region_sk(x, y), bin_index(x, y, CELL_GRID))
                                          for mid, fi, ei, x, y in chunk], 5)
            con.execute("""UPDATE fact_kill SET region_sk = zone_map.region_sk FROM zone_map
                WHERE fact_kill.match_id = zone_map.match_id AND fact_kill.frame_index = zone_map.frame_index
                AND fact_kill.event_index = zone_map.event_index""")
            duplicate = con.execute("SELECT count(*) FROM (SELECT match_id,frame_index,event_index "
                                    "FROM fact_kill GROUP BY ALL HAVING count(*) > 1)").fetchone()[0]
            duplicate_matches = con.execute("SELECT count(*)-count(DISTINCT match_id) FROM dim_match").fetchone()[0]
            invalid_refs = con.execute("""SELECT count(*) FROM fact_kill f
                LEFT JOIN dim_participant v ON f.match_id=v.match_id AND f.victim_pid=v.participant_id
                LEFT JOIN dim_participant k ON f.match_id=k.match_id AND f.killer_pid=k.participant_id
                WHERE v.participant_id IS NULL OR (f.killer_pid != 0 AND k.participant_id IS NULL)""").fetchone()[0]
            if duplicate or duplicate_matches or invalid_refs:
                raise ValidationError("Duplicate identities or broken participant references prevent publication")

            # -- dictionaries --------------------------------------------------
            # Key maps are deterministic and are independent of collection
            # order. Match keys are local to their partition, so each part's
            # rows are still grouped by game and the browser's distinct-game
            # trick holds per part.
            con.execute("CREATE TABLE patch_map AS SELECT patch,row_number() OVER (ORDER BY patch)-1 AS sk FROM (SELECT DISTINCT patch FROM dim_match)")
            con.execute(f"""CREATE TABLE match_map AS SELECT match_id, part_key,
                row_number() OVER (PARTITION BY part_key ORDER BY match_id)-1 AS sk FROM (
                SELECT dm.match_id, coalesce(tm.sk, {NULL_U8})::VARCHAR || '-' || dm.patch AS part_key
                FROM dim_match dm LEFT JOIN tier_map tm ON tm.tier = dm.tier)""")
            con.execute("""CREATE TABLE player_map AS SELECT puuid, display_name,
                row_number() OVER (ORDER BY puuid)-1 AS sk FROM (
                SELECT puuid, arg_max(display_name, match_id) AS display_name
                FROM dim_participant GROUP BY puuid)""")
            if con.execute("SELECT count(*) FROM player_map").fetchone()[0] >= NULL_U32:
                raise ValidationError("Player dictionary exceeds the current browser uint32 format")
            if con.execute("SELECT count(*) FROM patch_map").fetchone()[0] >= 255:
                raise ValidationError("Patch dictionary exceeds the current browser uint8 format")
            select = []
            for name, _ in COLUMNS:
                source = {"match_sk": "mm.sk", "patch_sk": "pm.sk", "victim_player": "vp.sk",
                          "killer_player": f"coalesce(kp.sk,{NULL_U32})",
                          "victim_tier": f"coalesce(vt.sk,{NULL_U8})",
                          "killer_tier": f"coalesce(kt.sk,{NULL_U8})",
                          "region_sk": "z.region_sk"}.get(name, f'f."{name}"')
                select.append(f'{source} AS "{name}"')
            con.execute("CREATE TABLE browser AS SELECT " + ",".join(select) + """, f.frame_index, f.event_index,
                z.cell128, mm.part_key, coalesce(mt.sk, 255) AS tier_sk
                FROM fact_kill f JOIN match_map mm USING(match_id)
                JOIN zone_map z ON f.match_id=z.match_id AND f.frame_index=z.frame_index AND f.event_index=z.event_index
                JOIN dim_match dm USING(match_id) JOIN patch_map pm USING(patch)
                LEFT JOIN tier_map mt ON mt.tier = dm.tier
                JOIN dim_participant v ON f.match_id=v.match_id AND f.victim_pid=v.participant_id
                JOIN player_map vp ON v.puuid=vp.puuid
                LEFT JOIN tier_map vt ON vt.tier = v.tier
                LEFT JOIN dim_participant k ON f.match_id=k.match_id AND f.killer_pid=k.participant_id
                LEFT JOIN player_map kp ON k.puuid=kp.puuid
                LEFT JOIN tier_map kt ON kt.tier = k.tier""")
            count = con.execute("SELECT count(*) FROM fact_kill").fetchone()[0]
            if con.execute("SELECT count(*) FROM browser").fetchone()[0] != count:
                raise ValidationError("Browser joins changed the fact count")
            # A re-seeded roster that attributes every game the same way
            # reproduces the id; a tier change on any participant mints a new
            # release, because the partitions and cells would differ.
            dataset_id = "v1-" + digest(json_bytes({"format": RELEASE_FORMAT_VERSION,
                                                   "schema": SCHEMA_VERSION,
                                                   "zones": ZONE_DIGEST,
                                                   "inputs": inventory,
                                                   "attribution": attribution_sha256}))[:20]
            release = work / "release"
            release.mkdir()
            patches = [r[0] for r in con.execute("SELECT patch FROM patch_map ORDER BY sk").fetchall()]
            champions = [r[0] for r in con.execute("SELECT DISTINCT champion_id FROM dim_participant ORDER BY champion_id").fetchall()]
            collected_from, collected_to = con.execute("SELECT min(collected_at),max(collected_at) FROM dim_match").fetchone()
            game_from, game_to = con.execute("SELECT min(game_start_ms),max(game_start_ms) FROM dim_match").fetchone()
            match_count, max_duration = con.execute("SELECT count(*), max(duration) FROM dim_match").fetchone()
            unknown_tier = con.execute("SELECT count(*) FROM dim_match WHERE tier IS NULL").fetchone()[0]

            # -- partitions ------------------------------------------------------
            core_columns = [c for c in COLUMNS if c[0] in CORE_COLUMNS]
            extended_columns = [c for c in COLUMNS if c[0] not in CORE_COLUMNS]
            parts_manifest = []
            for part_key, tier_sk, patch_sk, matches in con.execute(f"""
                    SELECT mm.part_key, coalesce(tm.sk, {NULL_U8}), pm.sk, count(*)
                    FROM match_map mm JOIN dim_match dm USING(match_id) JOIN patch_map pm USING(patch)
                    LEFT JOIN tier_map tm ON tm.tier = dm.tier
                    GROUP BY ALL ORDER BY 2, 3""").fetchall():
                folder = release / "parts" / part_key
                folder.mkdir(parents=True)
                con.execute("DROP TABLE IF EXISTS part_rows")
                con.execute("""CREATE TABLE part_rows AS SELECT *, row_number() OVER
                    (ORDER BY match_sk, frame_index, event_index) - 1 AS pos FROM browser WHERE part_key = ?""",
                            [part_key])
                rows = con.execute("SELECT count(*) FROM part_rows").fetchone()[0]
                part_matches = [dict(zip(("id", "patch", "duration", "blue_win", "tier"), r)) for r in con.execute(
                    f"""SELECT dm.match_id, pm.sk, dm.duration, dm.blue_win, coalesce(tm.sk, {NULL_U8})
                    FROM match_map mm JOIN dim_match dm USING(match_id) JOIN patch_map pm USING(patch)
                    LEFT JOIN tier_map tm ON tm.tier = dm.tier WHERE mm.part_key = ? ORDER BY mm.sk""",
                    [part_key]).fetchall()]
                (folder / "matches.json").write_bytes(json_bytes(part_matches))
                core = _binary(con, folder / "core.bin", core_columns, rows, "part_rows")
                extended = _binary(con, folder / "extended.bin", extended_columns, rows, "part_rows")
                parts_manifest.append({
                    "key": part_key, "tier": tier_sk, "patch": patch_sk, "rows": rows, "matches": matches,
                    "core": dict(core, file=f"parts/{part_key}/core.bin"),
                    "extended": dict(extended, file=f"parts/{part_key}/extended.bin"),
                    "matches_file": f"parts/{part_key}/matches.json"})
            con.execute("DROP TABLE IF EXISTS part_rows")

            # -- prerendered cells -----------------------------------------------
            # The default view (every filter empty, any set of tiers) is an
            # aggregation the browser can paint before a single row arrives.
            # Deaths and kills nest exactly across the rollups, so only the
            # finest grid is stored for them; distinct games do not, so each
            # rollup stores its own game count. Everything is per tier, and
            # tiers are disjoint sets of games, so the browser sums sections.
            cells_manifest = []
            regions = len(REGION_NAMES)
            with (release / "cells.bin").open("wb") as out:
                for (tier_sk,) in con.execute("SELECT DISTINCT tier_sk FROM browser ORDER BY 1").fetchall():
                    section = {"tier": tier_sk, "columns": []}
                    totals = con.execute("""SELECT count(*), count(*) FILTER (WHERE cause = 0),
                        count(DISTINCT match_id), count(DISTINCT match_id) FILTER (WHERE cause = 0)
                        FROM browser JOIN (SELECT match_id, part_key, sk AS match_sk FROM match_map) mm
                        USING (part_key, match_sk) WHERE tier_sk = ?""", [tier_sk]).fetchone()
                    section.update(rows=totals[0], kills=totals[1], death_matches=totals[2], kill_matches=totals[3])
                    section["matches"] = con.execute(f"""SELECT count(*) FROM dim_match dm
                        LEFT JOIN tier_map tm ON tm.tier = dm.tier WHERE coalesce(tm.sk, {NULL_U8}) = ?""",
                                                     [tier_sk]).fetchone()[0]
                    grid = CELL_GRID * CELL_GRID
                    deaths, kills = array.array("I", [0]) * grid, array.array("I", [0]) * grid
                    for cell, d, k in con.execute("""SELECT cell128, count(*), count(*) FILTER (WHERE cause = 0)
                            FROM browser WHERE tier_sk = ? GROUP BY cell128""", [tier_sk]).fetchall():
                        deaths[cell], kills[cell] = d, k
                    _write_column(out, section["columns"], f"deaths_{CELL_GRID}", "u32", deaths)
                    _write_column(out, section["columns"], f"kills_{CELL_GRID}", "u32", kills)
                    for size in ROLLUP_SIZES:
                        factor = CELL_GRID // size
                        games = array.array("I", [0]) * (size * size)
                        for cell, n in con.execute(f"""SELECT (cell128 // {CELL_GRID} // {factor}) * {size}
                                + (cell128 % {CELL_GRID}) // {factor} AS cell, count(DISTINCT part_key || ':' || match_sk)
                                FROM browser WHERE tier_sk = ? GROUP BY cell""", [tier_sk]).fetchall():
                            games[cell] = n
                        _write_column(out, section["columns"], f"games_{size}", "u32", games)
                    zone = {name: array.array("I", [0]) * regions for name in ("deaths", "kills", "games")}
                    for region, d, k, g in con.execute("""SELECT region_sk, count(*), count(*) FILTER (WHERE cause = 0),
                            count(DISTINCT part_key || ':' || match_sk) FROM browser WHERE tier_sk = ?
                            GROUP BY region_sk""", [tier_sk]).fetchall():
                        zone["deaths"][region], zone["kills"][region], zone["games"][region] = d, k, g
                    for name in ("deaths", "kills", "games"):
                        _write_column(out, section["columns"], f"zone_{name}", "u32", zone[name])
                    cells_manifest.append(section)

            # -- players -----------------------------------------------------------
            # player_map is ordered by PUUID, so each shard is a contiguous
            # sorted range and a PUUID is located by binary search over the
            # first id of every shard, then within the one shard fetched.
            players_dir = release / "players"
            players_dir.mkdir()
            player_count = con.execute("SELECT count(*) FROM player_map").fetchone()[0]
            first_ids, tiers = [], array.array("B", [NULL_U8]) * player_count
            cursor = con.cursor().execute(f"""SELECT pm.sk, pm.puuid, pm.display_name, coalesce(tm.sk, {NULL_U8})
                FROM player_map pm LEFT JOIN dim_player dp ON dp.puuid = pm.puuid
                LEFT JOIN tier_map tm ON tm.tier = dp.tier ORDER BY pm.sk""")
            shard = 0
            while chunk := cursor.fetchmany(PLAYER_SHARD):
                first_ids.append(chunk[0][1])
                (players_dir / f"names-{shard}.json").write_bytes(json_bytes([r[2] for r in chunk]))
                (players_dir / f"ids-{shard}.json").write_bytes(json_bytes([r[1] for r in chunk]))
                for sk, _, _, tier_sk in chunk:
                    tiers[sk] = tier_sk
                shard += 1
            (players_dir / "index.json").write_bytes(json_bytes(
                {"count": player_count, "shard_size": PLAYER_SHARD, "first_id": first_ids}))
            (players_dir / "tiers.bin").write_bytes(tiers.tobytes())

            present_tiers = sorted({p["tier"] for p in parts_manifest})
            manifest = {"version": MANIFEST_VERSION, "rows": count, "source_kind": "riot",
                        "meta": {"source_kind": "riot", "dataset_id": dataset_id, "schema_version": SCHEMA_VERSION,
                                 "format": RELEASE_FORMAT_VERSION,
                                 "match_count": match_count, "max_duration": max_duration or 0,
                                 "roles": ROLES, "causes": CAUSE_NAMES, "patches": patches,
                                 "tier_names": TIER_NAMES, "tiers": present_tiers,
                                 "unknown_tier_matches": unknown_tier,
                                 "grid": GRID_SIZE, "bytes_per_row": bytes_per_row(),
                                 "collected_from": collected_from, "collected_to": collected_to,
                                 "game_start_from": game_from, "game_start_to": game_to,
                                 "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(collected_to))},
                        "cells": {"file": "cells.bin", "bytes": (release / "cells.bin").stat().st_size,
                                  "grid": CELL_GRID, "rollups": list(ROLLUP_SIZES), "regions": regions,
                                  "tiers": cells_manifest},
                        "parts": parts_manifest,
                        "players": {"count": player_count, "shard_size": PLAYER_SHARD, "shards": shard,
                                    "index": "players/index.json",
                                    "tiers": {"file": "players/tiers.bin", "bytes": player_count}}}
            sidecars = {"champions": champions,
                        "regions": [{"name": n, "group": region_group(n)} for n in REGION_NAMES],
                        "manifest": manifest,
                        "validation": {"source_kind": "riot", "dataset_id": dataset_id,
                                       "matches": match_count, "facts": count, "duplicates": 0,
                                       "broken_references": 0, "roster_players": len(roster or []),
                                       "attribution_sha256": attribution_sha256,
                                       "unknown_tier_matches": unknown_tier,
                                       "parts": [{"key": p["key"], "matches": p["matches"], "rows": p["rows"]}
                                                 for p in parts_manifest],
                                       "inputs": inventory}}
            for name, payload in sidecars.items():
                (release / f"{name}.json").write_bytes(json_bytes(payload))
            # Relational snapshots power Athena; every table is partitioned by
            # release so queries can select exactly the published dataset.
            for table in WAREHOUSE_TABLES:
                path = work / f"{table}.parquet"
                order = {"fact_kill": "match_id,frame_index,event_index", "dim_player": "puuid"}.get(table, "match_id")
                con.execute(f"COPY (SELECT * FROM {table} ORDER BY {order}) TO {sql_path(path)} "
                            "(FORMAT PARQUET, COMPRESSION ZSTD)")
                try:
                    store.put(f"warehouse/{table}/dataset_id={dataset_id}/part-00000.parquet",
                              path.read_bytes(), immutable=True)
                except ValidationError:
                    # The release id covers inputs and tier attribution, which
                    # is everything the browser assets and fact_kill/dim_match
                    # depend on. LP, division and observation times are not
                    # part of it, so a re-seeded roster can rebuild the same
                    # release with fresher values in these two tables; the
                    # snapshot taken when the release was first built stands.
                    if table not in ("dim_participant", "dim_player"):
                        raise
            base = f"data/releases/{dataset_id}"
            for path in sorted(p for p in release.rglob("*") if p.is_file()):
                payload = path.read_bytes()
                mime = "application/json" if path.suffix == ".json" else "application/octet-stream"
                # S3 serves precompressed objects with Content-Encoding; local
                # HTTP servers serve the same original bytes without special setup.
                compressed = isinstance(site, S3Objects)
                if compressed:
                    payload = gzip.compress(payload, mtime=0)
                site.put(f"{base}/{path.relative_to(release).as_posix()}", payload, immutable=True,
                         content_type=mime, encoding="gzip" if compressed else None)
            if before_publish:
                before_publish()
            pointer = {"dataset_id": dataset_id, "base": base, "source_kind": "riot"}
            site.put("data/current.json", json_bytes(pointer), content_type="application/json")
            return {"dataset_id": dataset_id, "count": match_count, "rows": count, "source_kind": "riot"}
        finally:
            con.close()
