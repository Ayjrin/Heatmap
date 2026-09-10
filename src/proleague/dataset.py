"""Build and atomically publish live datasets from committed curated facts."""
from __future__ import annotations

import array
import gzip
import json
import sys
import tempfile
import time
from pathlib import Path

import duckdb

from .config import CAUSE_NAMES, GRID_SIZE, NULL_U16, ROLES
from .curated import (CURATED_PREFIX, FACT_COLUMNS, SCHEMA_VERSION, S3Objects,
                      ValidationError, digest, json_bytes, sql_path)
from .serve.bundle import TYPES, bytes_per_row
from .transform.kills import COLUMNS, CORE_COLUMNS, build_participants
from .transform.regions import REGION_NAMES, region_group

DIM_MATCH_COLUMNS = [("match_id", "VARCHAR"), ("patch", "VARCHAR"), ("duration", "BIGINT"),
                     ("blue_win", "BOOLEAN"), ("game_start_ms", "BIGINT"), ("collected_at", "BIGINT")]
DIM_PARTICIPANT_COLUMNS = [("match_id", "VARCHAR"), ("participant_id", "BIGINT"),
                          ("puuid", "VARCHAR"), ("display_name", "VARCHAR"),
                          ("champion_id", "BIGINT"), ("team_id", "BIGINT"),
                          ("role", "BIGINT"), ("won", "BOOLEAN")]

# Bump whenever the release serialization or dictionary semantics change.
RELEASE_FORMAT_VERSION = 2


def _make_table(con, name, columns):
    con.execute(f"CREATE TABLE {name} (" + ",".join(f'"{n}" {t}' for n, t in columns) + ")")


def _insert(con, name, rows, width):
    if rows:
        con.executemany(f"INSERT INTO {name} VALUES (" + ",".join("?" for _ in range(width)) + ")", rows)


def _binary(con, path, columns, count):
    entries = []
    with path.open("wb") as out:
        for name, kind in columns:
            pad = (-out.tell()) % 8
            out.write(b"\0" * pad)
            entries.append({"name": name, "type": kind, "js": TYPES[kind][2],
                            "offset": out.tell(), "length": count})
            cur = con.execute(f'SELECT "{name}" FROM browser ORDER BY match_sk, frame_index, event_index')
            while chunk := cur.fetchmany(8192):
                values = array.array(TYPES[kind][0], (row[0] for row in chunk))
                if sys.byteorder != "little":
                    values.byteswap()
                out.write(values.tobytes())
    return {"file": path.name, "bytes": path.stat().st_size, "columns": entries}


def build_release(store, site, *, run_id="rebuild", before_publish=None):
    """Validate all curated inputs, upload immutable outputs, then switch pointer.

    DuckDB spills its working set to a temporary directory. Facts and wire
    columns are streamed in bounded batches; raw Riot data is never needed.
    """
    from .curated import game_duration_seconds

    keys = sorted(k for k in store.keys(CURATED_PREFIX) if k.endswith("/complete.json"))
    if not keys:
        raise ValidationError("No completed live Riot games are available")
    inventory = []
    with tempfile.TemporaryDirectory(prefix="heatmap-build-") as tmp:
        work = Path(tmp)
        con = duckdb.connect(str(work / "build.duckdb"))
        try:
            con.execute("SET memory_limit='512MB'")
            con.execute(f"SET temp_directory={sql_path(work / 'spill')}")
            _make_table(con, "fact_kill", FACT_COLUMNS)
            _make_table(con, "dim_match", DIM_MATCH_COLUMNS)
            _make_table(con, "dim_participant", DIM_PARTICIPANT_COLUMNS)
            for key in keys:
                complete = json.loads(store.get(key))
                if complete.get("source_kind") != "riot" or complete.get("schema_version") != SCHEMA_VERSION:
                    raise ValidationError("Publication requires live Riot provenance and the current schema")
                prefix = key.rsplit("/", 1)[0] + "/"
                context_raw, facts_raw = store.get(prefix + "context.json"), store.get(prefix + "facts.parquet")
                if context_raw is None or facts_raw is None:
                    raise ValidationError("A completed match is missing a required curated object")
                if digest(context_raw) != complete["context_sha256"] or digest(facts_raw) != complete["facts_sha256"]:
                    raise ValidationError("Curated input failed its integrity check")
                context = json.loads(context_raw)
                mid = context["metadata"]["matchId"]
                if context.get("source_kind") != "riot" or mid != complete["match_id"]:
                    raise ValidationError("Curated context provenance or identity is inconsistent")
                parquet = work / "one-match.parquet"
                parquet.write_bytes(facts_raw)
                count, invalid = con.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE match_id != ?) FROM read_parquet({sql_path(parquet)})",
                    [mid]).fetchone()
                if invalid or count != complete["row_count"]:
                    raise ValidationError("Curated facts do not reconcile with the match commit")
                names = ",".join(f'"{n}"' for n, _ in FACT_COLUMNS)
                con.execute(f"INSERT INTO fact_kill SELECT {names} FROM read_parquet({sql_path(parquet)})")
                info = context["info"]
                parts = build_participants(context)
                blue = next(p for p in parts.values() if p.team_id == 100)
                patch = ".".join(str(info.get("gameVersion", "")).split(".")[:2])
                start = int(info.get("gameStartTimestamp") or info.get("gameCreation") or 0)
                _insert(con, "dim_match", [(mid, patch, int(game_duration_seconds(info)), blue.won,
                                            start, complete["collected_at"])], len(DIM_MATCH_COLUMNS))
                _insert(con, "dim_participant", [
                    (mid, p.pid, p.puuid, p.riot_id or f"Player {p.puuid[:8]}",
                     p.champ_id, p.team_id, p.role_sk, p.won) for p in parts.values()], len(DIM_PARTICIPANT_COLUMNS))
                inventory.append({"match_id": mid, "context_sha256": digest(context_raw),
                                  "facts_sha256": digest(facts_raw), "rows": count})
            duplicate = con.execute("SELECT count(*) FROM (SELECT match_id,frame_index,event_index "
                                    "FROM fact_kill GROUP BY ALL HAVING count(*) > 1)").fetchone()[0]
            duplicate_matches = con.execute("SELECT count(*)-count(DISTINCT match_id) FROM dim_match").fetchone()[0]
            invalid_refs = con.execute("""SELECT count(*) FROM fact_kill f
                LEFT JOIN dim_participant v ON f.match_id=v.match_id AND f.victim_pid=v.participant_id
                LEFT JOIN dim_participant k ON f.match_id=k.match_id AND f.killer_pid=k.participant_id
                WHERE v.participant_id IS NULL OR (f.killer_pid != 0 AND k.participant_id IS NULL)""").fetchone()[0]
            if duplicate or duplicate_matches or invalid_refs:
                raise ValidationError("Duplicate identities or broken participant references prevent publication")
            # Key maps are deterministic and are independent of collection order.
            con.execute("CREATE TABLE match_map AS SELECT match_id,row_number() OVER (ORDER BY match_id)-1 AS sk FROM dim_match")
            con.execute("CREATE TABLE patch_map AS SELECT patch,row_number() OVER (ORDER BY patch)-1 AS sk FROM (SELECT DISTINCT patch FROM dim_match)")
            con.execute("""CREATE TABLE player_map AS SELECT puuid, display_name,
                row_number() OVER (ORDER BY puuid)-1 AS sk FROM (
                SELECT puuid, arg_max(display_name, match_id) AS display_name
                FROM dim_participant GROUP BY puuid)""")
            if con.execute("SELECT count(*) FROM player_map").fetchone()[0] >= NULL_U16:
                raise ValidationError("Player dictionary exceeds the current browser uint16 format")
            if con.execute("SELECT count(*) FROM patch_map").fetchone()[0] >= 255:
                raise ValidationError("Patch dictionary exceeds the current browser uint8 format")
            select = []
            for name, _ in COLUMNS:
                source = {"match_sk": "mm.sk", "patch_sk": "pm.sk", "victim_player": "vp.sk",
                          "killer_player": f"coalesce(kp.sk,{NULL_U16})"}.get(name, f'f."{name}"')
                select.append(f'{source} AS "{name}"')
            con.execute("CREATE TABLE browser AS SELECT " + ",".join(select) + """, f.frame_index,f.event_index
                FROM fact_kill f JOIN match_map mm USING(match_id)
                JOIN dim_match dm USING(match_id) JOIN patch_map pm USING(patch)
                JOIN dim_participant v ON f.match_id=v.match_id AND f.victim_pid=v.participant_id
                JOIN player_map vp ON v.puuid=vp.puuid
                LEFT JOIN dim_participant k ON f.match_id=k.match_id AND f.killer_pid=k.participant_id
                LEFT JOIN player_map kp ON k.puuid=kp.puuid""")
            count = con.execute("SELECT count(*) FROM fact_kill").fetchone()[0]
            if con.execute("SELECT count(*) FROM browser").fetchone()[0] != count:
                raise ValidationError("Browser joins changed the fact count")
            dataset_id = "v1-" + digest(json_bytes({"format": RELEASE_FORMAT_VERSION,
                                                   "schema": SCHEMA_VERSION,
                                                   "inputs": inventory}))[:20]
            release = work / "release"
            release.mkdir()
            patches = [r[0] for r in con.execute("SELECT patch FROM patch_map ORDER BY sk").fetchall()]
            players = [{"id": r[0], "name": r[1]} for r in con.execute(
                "SELECT puuid,display_name FROM player_map ORDER BY sk").fetchall()]
            champions = [r[0] for r in con.execute("SELECT DISTINCT champion_id FROM dim_participant ORDER BY champion_id").fetchall()]
            matches = [dict(zip(("id", "patch", "duration", "blue_win"), r)) for r in con.execute(
                "SELECT dm.match_id,pm.sk,dm.duration,dm.blue_win FROM dim_match dm JOIN patch_map pm USING(patch) "
                "ORDER BY dm.match_id").fetchall()]
            collected_from, collected_to = con.execute("SELECT min(collected_at),max(collected_at) FROM dim_match").fetchone()
            game_from, game_to = con.execute("SELECT min(game_start_ms),max(game_start_ms) FROM dim_match").fetchone()
            manifest = {"version": 1, "rows": count, "source_kind": "riot",
                        "core": _binary(con, release / "core.bin", [c for c in COLUMNS if c[0] in CORE_COLUMNS], count),
                        "extended": _binary(con, release / "extended.bin", [c for c in COLUMNS if c[0] not in CORE_COLUMNS], count),
                        "meta": {"source_kind": "riot", "dataset_id": dataset_id, "schema_version": SCHEMA_VERSION,
                                 "match_count": len(matches), "roles": ROLES, "causes": CAUSE_NAMES,
                                 "patches": patches, "grid": GRID_SIZE, "bytes_per_row": bytes_per_row(),
                                 "collected_from": collected_from, "collected_to": collected_to,
                                 "game_start_from": game_from, "game_start_to": game_to,
                                 "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(collected_to))}}
            sidecars = {"matches": matches, "players": players, "champions": champions,
                        "regions": [{"name": n, "group": region_group(n)} for n in REGION_NAMES],
                        "manifest": manifest,
                        "validation": {"source_kind": "riot", "dataset_id": dataset_id,
                                       "matches": len(matches), "facts": count, "duplicates": 0,
                                       "broken_references": 0, "inputs": inventory}}
            for name, payload in sidecars.items():
                (release / f"{name}.json").write_bytes(json_bytes(payload))
            # Relational snapshots power Athena; every table is partitioned by
            # release so queries can select exactly the published dataset.
            for table in ("fact_kill", "dim_match", "dim_participant"):
                path = work / f"{table}.parquet"
                order = "match_id,frame_index,event_index" if table == "fact_kill" else "match_id"
                con.execute(f"COPY (SELECT * FROM {table} ORDER BY {order}) TO {sql_path(path)} "
                            "(FORMAT PARQUET, COMPRESSION ZSTD)")
                store.put(f"warehouse/{table}/dataset_id={dataset_id}/part-00000.parquet", path.read_bytes(), immutable=True)
            base = f"data/releases/{dataset_id}"
            for path in sorted(release.iterdir()):
                payload = path.read_bytes()
                mime = "application/json" if path.suffix == ".json" else "application/octet-stream"
                # S3 serves precompressed objects with Content-Encoding; local
                # HTTP servers serve the same original bytes without special setup.
                compressed = isinstance(site, S3Objects)
                if compressed:
                    payload = gzip.compress(payload, mtime=0)
                site.put(f"{base}/{path.name}", payload, immutable=True, content_type=mime,
                         encoding="gzip" if compressed else None)
            if before_publish:
                before_publish()
            pointer = {"dataset_id": dataset_id, "base": base, "source_kind": "riot"}
            site.put("data/current.json", json_bytes(pointer), content_type="application/json")
            return {"dataset_id": dataset_id, "count": len(matches), "rows": count, "source_kind": "riot"}
        finally:
            con.close()
